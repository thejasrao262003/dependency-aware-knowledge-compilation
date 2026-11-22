"""The incremental compiler.

One build = scan -> compute (layer by layer, provider calls concurrent within
a layer) -> commit (one transaction) -> project. The compute phase never writes
knowledge to the store; it produces a :class:`ChangeSet`.

Strategies (see docs/benchmark.md):

``dakc``  passage-level verifying traces, content-addressed claims, signature
          checks and early cutoff at every layer.
``file``  a changed source is wholly dirty: all of its passages are
          re-extracted without cache and every page/summary reachable from its
          old or new claims is regenerated. Renames are delete + add. This
          emulates the file-level strategy of existing LLM-wiki tools.
``full``  discard derived state and cache, recompute everything (reference).
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

from dakc.artifacts import (
    INDEX_ID,
    Artifact,
    ClaimArt,
    EntityArt,
    IndexArt,
    PageArt,
    PassageArt,
    RelationshipArt,
    SummaryArt,
    Usage,
)
from dakc.claims import ExtractedClaim, match_revisions, plan_reuse, segment, sentence_key, split_sentences
from dakc.hashing import claim_id, entity_key, h, h_obj, normalize_claim, normalize_text, slugify
from dakc.ingestion import FileInfo, ScanResult, SourceRecord, new_source_id, scan
from dakc.parsing import ParseError, parse_bytes
from dakc.providers import ClaimView, PageLead, ProviderBundle
from dakc.provenance.trace import ground_body
from dakc.storage import KBState, Store
from dakc.transactions import ChangeSet, FaultInjector, commit
from dakc.transactions.lock import workspace_lock
from dakc import versions

STRATEGIES = ("dakc", "file", "full")
RELATED_LIMIT = 10
T = TypeVar("T")
R = TypeVar("R")


class SourceChangedDuringBuild(RuntimeError):
    pass


def topic_of(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "_root"


def page_id_for(entity_id: str) -> str:
    return "page:" + entity_id.split(":", 1)[1]


def summary_id_for(topic: str) -> str:
    return "sum:" + slugify(topic)


_FOOTNOTE = re.compile(r"\s*\[\^[^\]]+\]")


def first_paragraph(body: str) -> str:
    """The page *lead*: first non-heading paragraph, without citation markers."""
    for para in body.strip().split("\n\n"):
        para = para.strip()
        if para and not para.startswith("#"):
            return _FOOTNOTE.sub("", para).strip()
    return ""


@dataclass
class BuildReport:
    strategy: str
    dry_run: bool
    exact: bool = True
    build_id: int | None = None
    status: str = "planned"
    scan: dict[str, Any] = field(default_factory=dict)
    ops: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    counts: dict[str, int] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    planned_llm_calls: dict[str, int] = field(default_factory=dict)
    changes: dict[str, int] = field(default_factory=dict)
    revisions: list[tuple[str, str, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    projection: dict[str, int] = field(default_factory=dict)

    def op(self, category: str, ident: str) -> None:
        self.ops[category].append(ident)

    def n(self, category: str) -> int:
        return len(self.ops.get(category, []))

    def llm_calls(self) -> int:
        return self.usage.llm_calls + sum(self.planned_llm_calls.values())

    def to_dict(self, limit: int | None = None) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "dry_run": self.dry_run,
            "exact": self.exact,
            "build_id": self.build_id,
            "status": self.status,
            "scan": {k: (v[:limit] if limit and isinstance(v, list) else v) for k, v in self.scan.items()},
            "operations": {k: sorted(v)[:limit] for k, v in sorted(self.ops.items())},
            "operation_counts": {k: len(v) for k, v in sorted(self.ops.items())},
            "counts": self.counts,
            "usage": self.usage.as_dict(),
            "planned_llm_calls": self.planned_llm_calls,
            "changes": self.changes,
            "claim_revisions": [{"old": o, "new": n, "score": round(s, 1)} for o, n, s in self.revisions],
            "notes": self.notes,
            "duration_s": round(self.duration_s, 4),
            "projection": self.projection,
        }


class Compiler:
    def __init__(
        self,
        store: Store,
        provider: ProviderBundle,
        source_root: Path,
        *,
        aliases: dict[str, str] | None = None,
        config_fingerprint: str = "",
        strategy: str = "dakc",
        jobs: int = 4,
        dry_run: bool = False,
        fault: FaultInjector | None = None,
        exclude: tuple[Path, ...] = (),
        sentence_reuse: bool = True,
        acquire_lock: bool = True,
        lock_timeout: float = 0.0,
        provenance: str = "report",
    ):
        if strategy not in STRATEGIES:
            raise ValueError(f"strategy must be one of {STRATEGIES}")
        self.store = store
        self.provider = provider
        self.source_root = source_root
        self.alias_map = aliases or {}
        if provenance not in ("report", "strict"):
            raise ValueError("provenance must be 'report' or 'strict'")
        self.provenance = provenance
        self.config_fingerprint = config_fingerprint or h_obj(
            {"aliases": self.alias_map, "entity_resolution": versions.ENTITY_RESOLUTION, "provenance": provenance}
        )
        self.strategy = strategy
        self.jobs = max(1, jobs)
        self.dry_run = dry_run
        self.fault = fault or FaultInjector()
        self.exclude = exclude
        self.forced = strategy in ("file", "full")
        self.sentence_reuse = sentence_reuse
        self.acquire_lock = acquire_lock
        self.lock_timeout = lock_timeout

    # ================================================================== run
    def run(self) -> tuple[BuildReport, KBState | None]:
        """Compile. Holds the workspace build lock unless ``acquire_lock=False``
        (the caller already holds it) or this is a dry run (read-only)."""
        if self.dry_run or not self.acquire_lock:
            return self._run()
        with workspace_lock(self.store.path, self.lock_timeout):
            return self._run()

    def _run(self) -> tuple[BuildReport, KBState | None]:
        t0 = time.perf_counter()
        report = BuildReport(self.strategy, self.dry_run)
        if not self.dry_run:
            self.store.recover_aborted_builds()  # safe: we hold the build lock
        head_at_load = self.store.head
        old = self.store.load_state()
        result = scan(self.source_root, old.sources, detect_renames=self.strategy != "file", exclude=self.exclude)
        report.scan = {
            **result.summary(),
            "new_paths": result.new,
            "modified_paths": [p for _, p in result.modified],
            "deleted_ids": result.deleted,
            "renamed_paths": [[o, n] for _, o, n in result.renamed],
            "duplicates": result.duplicates,
        }
        build_id = None if self.dry_run else self.store.begin_build(self.strategy, "dry-run" if self.dry_run else "compile")
        report.build_id = build_id
        try:
            cs, new_state = self._compute(old, result, report)
            report.changes = cs.counts()
            stats = {
                **report.to_dict(limit=200),
                "config_fingerprint": self.config_fingerprint,
                "provider": self.provider.name,
                "fingerprints": self.provider.fingerprints(),
                "compiler_versions": versions.current(),
            }
            if self.dry_run:
                report.status = "planned"
            elif cs.empty and old.config_fingerprint == self.config_fingerprint and old.compiler_versions == versions.current():
                report.status = "noop"
                report.duration_s = time.perf_counter() - t0
                stats["duration_s"] = round(report.duration_s, 4)
                self.store.finish_build(build_id, "noop", stats)
            else:
                cs.meta["config_fingerprint"] = self.config_fingerprint
                cs.meta["compiler_versions"] = json.dumps(versions.current(), sort_keys=True)
                report.duration_s = time.perf_counter() - t0
                stats["duration_s"] = round(report.duration_s, 4)
                commit(self.store, build_id, cs, old.versions, stats, self.fault, expected_head=head_at_load)
                report.status = "committed"
        except BaseException as exc:
            report.status = "failed"
            if build_id is not None:
                self.store.finish_build(build_id, "failed", {"strategy": self.strategy}, error=f"{type(exc).__name__}: {exc}")
            raise
        report.duration_s = time.perf_counter() - t0
        return report, (None if self.dry_run else new_state)

    # ============================================================== compute
    def _compute(self, old: KBState, sr: ScanResult, report: BuildReport) -> tuple[ChangeSet, KBState]:
        full = self.strategy == "full"
        # A store with history but no recorded versions predates versioning: treat every layer as outdated.
        has_history = bool(old.sources) or any(old.artifacts.values())
        stored_v = old.compiler_versions if old.compiler_versions or not has_history else {k: "unknown" for k in versions.current()}
        parser_changed = has_history and not full and stored_v.get("parser") != versions.PARSER
        identity_changed = has_history and not full and stored_v.get("claim_identity") != versions.CLAIM_IDENTITY
        render_changed = has_history and not full and stored_v.get("render") != versions.RENDER
        for layer, changed in (("parser", parser_changed), ("claim identity", identity_changed), ("rendering", render_changed)):
            if changed:
                report.notes.append(f"{layer} logic changed since HEAD ({stored_v.get(layer.replace(' ', '_').replace('rendering', 'render'))} -> current): affected layer recomputed")
        P = self.provider
        base_passages: dict[str, PassageArt] = {} if full else dict(old.of("passage"))  # type: ignore[arg-type]
        base_claims: dict[str, ClaimArt] = {} if full else dict(old.of("claim"))  # type: ignore[arg-type]
        passages = dict(base_passages)
        claims = dict(base_claims)
        entities: dict[str, EntityArt] = {} if full else dict(old.of("entity"))  # type: ignore[arg-type]
        rels: dict[str, RelationshipArt] = {} if full else dict(old.of("relationship"))  # type: ignore[arg-type]
        pages: dict[str, PageArt] = {} if full else dict(old.of("page"))  # type: ignore[arg-type]
        summaries: dict[str, SummaryArt] = {} if full else dict(old.of("summary"))  # type: ignore[arg-type]

        passages_by_source: dict[str, set[str]] = defaultdict(set)
        for p in passages.values():
            passages_by_source[p.source_id].add(p.id)
        claims_by_passage: dict[str, set[str]] = defaultdict(set)
        for c in claims.values():
            for pid in c.passage_ids:
                claims_by_passage[pid].add(c.id)

        # ------------------------------------------------------------ sources
        sources: dict[str, SourceRecord] = dict(old.sources)
        taken = self.store.all_source_ids()
        to_parse: list[tuple[str, FileInfo]] = []
        removed_pids: set[str] = set()
        renamed_sources: set[str] = set()

        for sid in sr.deleted:
            sources.pop(sid, None)
            removed_pids |= passages_by_source.get(sid, set())
            report.op("source_deleted", sid)
        for sid, _old_path, new_path in sr.renamed:
            sources[sid] = replace(old.sources[sid], path=new_path)
            renamed_sources.add(sid)
            report.op("source_renamed", sid)
            if full:
                to_parse.append((sid, sr.files[new_path]))
        for sid, path in sr.modified:
            to_parse.append((sid, sr.files[path]))
            report.op("source_modified", sid)
        for path in sr.new:
            sid = new_source_id(path, taken)
            to_parse.append((sid, sr.files[path]))
            report.op("source_new", sid)
        if full or parser_changed:
            for sid in sr.unchanged:
                to_parse.append((sid, sr.files[old.sources[sid].path]))

        added: dict[str, PassageArt] = {}
        gone_by_source: dict[str, set[str]] = defaultdict(set)  # replaced passage versions, for sentence reuse
        for sid, info in sorted(to_parse, key=lambda t: t[1].path):
            raw = info.abspath.read_bytes()
            if h(raw) != info.raw_hash:
                raise SourceChangedDuringBuild(f"{info.path} changed while compiling; run again")
            try:
                doc = parse_bytes(raw, info.source_type)
                new_passages = segment(sid, doc)
                text_hash = doc.text_hash
            except ParseError as exc:
                report.notes.append(f"parse error in {info.path}: {exc}")
                new_passages, text_hash = [], h(f"parse-error:{exc}")
            report.op("source_processed", sid)
            sources[sid] = SourceRecord(sid, info.path, info.raw_hash, text_hash, info.size, info.source_type)
            old_pids = passages_by_source.get(sid, set())
            new_by_id = {p.id: p for p in new_passages}
            if self.strategy == "file":
                gone, fresh = set(old_pids), set(new_by_id)
            else:
                gone, fresh = old_pids - set(new_by_id), set(new_by_id) - old_pids
            removed_pids |= gone
            gone_by_source[sid] |= gone
            for pid in fresh:
                p = new_by_id[pid]
                added[pid] = PassageArt(pid, sid, p.text, p.text_hash, p.heading, p.occurrence, P.extractor.fingerprint)
        report.counts["sources_processed"] = len(to_parse)

        # Passages whose claims came from a different extractor (model, prompt or
        # algorithm changed) are stale even though their text is unchanged.
        for pid in sorted(passages):
            p = passages[pid]
            if pid not in removed_pids and pid not in added and (p.extracted_by != P.extractor.fingerprint or identity_changed):
                removed_pids.add(pid)
                added[pid] = replace(p, extracted_by=P.extractor.fingerprint)
                report.op("extractor_changed" if p.extracted_by != P.extractor.fingerprint else "claim_identity_changed", pid)

        # --------------------------------------------------------- extraction
        ex = P.extractor

        def cache_key(text_hash: str) -> str:
            return h(f"{ex.fingerprint}\x00{text_hash}")

        reuse = self.sentence_reuse and not self.forced
        prior_keys_needed = {
            cache_key(base_passages[g].text_hash) for gs in gone_by_source.values() for g in gs if g in base_passages
        } if reuse else set()
        keys = {pid: cache_key(p.text_hash) for pid, p in added.items()}
        cached = {} if self.forced else self.store.cache_get_many(set(keys.values()) | prior_keys_needed)

        def from_cache(entries: list, text: str) -> list[ExtractedClaim]:
            pos = {}
            for i, x in enumerate(split_sentences(text)):
                pos.setdefault(sentence_key(x), i)
            return [ExtractedClaim(e[0], tuple(e[1]), tuple(sorted(pos[k] for k in (e[2] if len(e) > 2 else []) if k in pos))) for e in entries]

        def to_cache(claims_: list[ExtractedClaim], text: str) -> list:
            skeys = [sentence_key(x) for x in split_sentences(text)]
            return [[c.text, list(c.mentions), [skeys[i] for i in c.evidence if 0 <= i < len(skeys)]] for c in claims_]

        results: dict[str, list[ExtractedClaim] | None] = {}
        to_call: list[tuple[PassageArt, tuple[int, ...] | None, list[ExtractedClaim]]] = []
        pending: list[tuple[str, list]] = []
        n_sentences_reused = n_sentences_extracted = 0
        for pid in sorted(added):
            p = added[pid]
            if keys[pid] in cached:
                report.usage.cache_hits += 1
                results[pid] = from_cache(cached[keys[pid]], p.text)
                report.op("extraction_cache_hit", pid)
                continue
            targets: tuple[int, ...] | None = None
            reused: list[ExtractedClaim] = []
            if reuse and gone_by_source.get(p.source_id):
                prior, prior_sentence_keys = [], set()
                for g in gone_by_source[p.source_id]:
                    gp = base_passages.get(g)
                    entries = cached.get(cache_key(gp.text_hash)) if gp else None
                    if entries is None:
                        continue  # no evidence for this version: its sentences count as new
                    prior += [(e[0], tuple(e[1]), tuple(e[2])) for e in entries if len(e) > 2]
                    prior_sentence_keys |= {sentence_key(x) for x in split_sentences(gp.text)}
                if prior_sentence_keys:
                    reused, dirty = plan_reuse(prior, prior_sentence_keys, p.text)
                    targets = tuple(dirty)
                    n_sentences_reused += len(split_sentences(p.text)) - len(dirty)
            if targets == ():
                results[pid] = reused
                report.op("sentence_reuse_only", pid)
                if not self.dry_run:
                    pending.append((keys[pid], to_cache(reused, p.text)))
            else:
                n_sentences_extracted += len(targets) if targets is not None else len(split_sentences(p.text))
                to_call.append((p, targets, reused))
                report.op("reextract", pid)
        report.counts["sentences_reused"] = n_sentences_reused
        report.counts["sentences_extracted"] = n_sentences_extracted

        if self.dry_run and not ex.free:
            report.exact = False
            report.planned_llm_calls["extract"] = len(to_call)
            for p, _t, reused in to_call:
                results[p.id] = None if not reused else reused
        else:
            # Cache rows are content-addressed facts about provider output, not
            # knowledge: they are flushed even if the build later fails, so a
            # retry does not pay for the same extraction twice.
            try:
                for (p, targets, reused), res in self._map(lambda it: ex.extract(it[0].text, it[1]), to_call, ex.is_llm, "extract"):
                    report.usage.add("extract", llm=ex.is_llm, input_tokens=res.input_tokens, output_tokens=res.output_tokens, calls=getattr(res, "calls", 1))
                    seen = {claim_id(c.text) for c in reused}
                    merged = list(reused) + [c for c in res.claims if claim_id(c.text) not in seen]
                    results[p.id] = merged
                    report.counts["claims_from_provider"] = report.counts.get("claims_from_provider", 0) + len(res.claims)
                    if not self.dry_run:
                        pending.append((keys[p.id], to_cache(merged, p.text)))
                        if len(pending) >= 512:
                            self.store.cache_put_many(pending, ex.fingerprint)
                            pending.clear()
            finally:
                if pending and not self.dry_run:
                    self.store.cache_put_many(pending, ex.fingerprint)
        report.counts["claims_extracted"] = sum(len(v) for v in results.values() if v)
        report.counts.setdefault("claims_from_provider", 0)

        # ------------------------------------------------------------- claims
        touched_claims: set[str] = set()
        for pid in sorted(removed_pids):
            for cid in sorted(claims_by_passage.get(pid, ())):
                c = claims.get(cid)
                if c is None:
                    continue
                claims[cid] = ClaimArt(cid, tuple(s for s in c.supports if s[0] != pid))
                touched_claims.add(cid)
            passages.pop(pid, None)
            claims_by_passage.pop(pid, None)
        for pid in sorted(added):
            passages[pid] = added[pid]
            for ec in results.get(pid) or ():
                cid = claim_id(ec.text)
                prev = claims.get(cid)
                supports = {s[0]: s for s in prev.supports} if prev else {}
                supports[pid] = (pid, normalize_text(ec.text), tuple(sorted(set(ec.mentions))), tuple(sorted(set(ec.evidence))))
                claims[cid] = ClaimArt(cid, tuple(sorted(supports.values())))
                claims_by_passage[pid].add(cid)
                touched_claims.add(cid)
        for cid in sorted(touched_claims):
            if not claims[cid].supports:
                del claims[cid]
                if cid in base_claims:
                    report.op("retract_claim", cid)
            elif cid not in base_claims:
                report.op("new_claim", cid)
            elif claims[cid].state_hash != base_claims[cid].state_hash:
                report.op("claim_provenance_changed" if claims[cid].content_hash == base_claims[cid].content_hash else "claim_changed", cid)
            else:
                report.op("claim_reproduced", cid)
        retracted = {c: base_claims[c].text for c in report.ops.get("retract_claim", [])}
        created = {c: claims[c].text for c in report.ops.get("new_claim", [])}
        if retracted and created and len(retracted) * len(created) <= 40_000:
            report.revisions = [(retracted[o], created[n], s) for o, n, s in match_revisions(retracted, created)]

        # -------------------------------------------- entities & relationships
        config_changed = bool(old.config_fingerprint) and old.config_fingerprint != self.config_fingerprint and not full
        if config_changed:
            report.notes.append("configuration changed (aliases, entity resolution or provenance mode): entities re-resolved and pages re-checked")
        members: dict[str, set[str]] = {}
        rel_members: dict[str, set[str]] = {}
        touched_entities: set[str] = set()
        touched_rels: set[str] = set()

        def ent_members(eid: str) -> set[str]:
            if eid not in members:
                members[eid] = set(entities[eid].claim_ids) if eid in entities else set()
            return members[eid]

        def rel_set(rid: str) -> set[str]:
            if rid not in rel_members:
                rel_members[rid] = set(rels[rid].claim_ids) if rid in rels else set()
            return rel_members[rid]

        if config_changed:
            touched_entities |= set(entities)
            touched_rels |= set(rels)
            members = {e: set() for e in entities}
            rel_members = {r: set() for r in rels}
            assignments = [(None, c) for c in claims.values()]
        else:
            assignments = [(base_claims.get(cid), claims.get(cid)) for cid in sorted(touched_claims)]
        for before, after in assignments:
            old_e = self._entity_ids(before) if before is not None else set()
            new_e = self._entity_ids(after) if after is not None else set()
            cid = (after or before).id  # type: ignore[union-attr]
            for e in old_e - new_e:
                ent_members(e).discard(cid)
            for e in new_e - old_e:
                ent_members(e).add(cid)
            touched_entities |= old_e | new_e
            old_r, new_r = self._pairs(old_e), self._pairs(new_e)
            for r in old_r - new_r:
                rel_set(r).discard(cid)
            for r in new_r - old_r:
                rel_set(r).add(cid)
            touched_rels |= old_r | new_r

        rels_by_entity: dict[str, set[str]] = defaultdict(set)
        for r in rels.values():
            rels_by_entity[r.a].add(r.id)
            rels_by_entity[r.b].add(r.id)
        for rid in sorted(touched_rels):
            cids = rel_set(rid)
            a, b = self._rel_endpoints(rid)
            if cids:
                rels[rid] = RelationshipArt(rid, a, b, tuple(sorted(cids)))
                rels_by_entity[a].add(rid)
                rels_by_entity[b].add(rid)
            else:
                rels.pop(rid, None)
                rels_by_entity[a].discard(rid)
                rels_by_entity[b].discard(rid)

        name_changed: set[str] = set()
        for eid in sorted(touched_entities):
            cids = ent_members(eid)
            if not cids:
                if entities.pop(eid, None) is not None:
                    report.op("delete_entity", eid)
                continue
            name = self._display_name(eid, cids, claims)
            prev = entities.get(eid)
            entities[eid] = EntityArt(eid, name, tuple(sorted(cids)))
            if prev is None or prev.name != name:
                name_changed.add(eid)
            if prev is None or prev.state_hash != entities[eid].state_hash:
                report.op("reconcile_entity", eid)

        # -------------------------------------------------------------- pages
        candidates: set[str] = set(touched_entities)
        for rid in touched_rels:
            candidates |= set(self._rel_endpoints(rid))
        for eid in name_changed:
            for rid in rels_by_entity.get(eid, ()):
                if rid in rels:
                    candidates |= {rels[rid].a, rels[rid].b}
        if renamed_sources:
            moved = {pid for sid in renamed_sources for pid in passages_by_source.get(sid, ())}
            for pid in moved:
                for cid in claims_by_passage.get(pid, ()):
                    if cid in claims:
                        candidates |= self._entity_ids(claims[cid])
        if full or render_changed:
            candidates |= set(entities)
        # Pages written by a different writer (model, prompt) are stale even if no source changed.
        drifted = {p.entity_id for p in pages.values() if p.generator != P.writer.fingerprint}
        if drifted:
            report.notes.append(f"{len(drifted)} page(s) were written by a different page writer: regenerated")
            candidates |= drifted

        W = P.writer
        page_plans: list[tuple[str, EntityArt, list[str], str]] = []  # (page id, entity, claim order, gen_sig)
        regen: list[tuple[str, str, tuple[ClaimView, ...]]] = []
        for eid in sorted(candidates):
            pid = page_id_for(eid)
            ent = entities.get(eid)
            if ent is None:
                if pid in pages:
                    del pages[pid]
                    report.op("delete_page", pid)
                continue
            order = sorted(ent.claim_ids, key=lambda c: (normalize_claim(claims[c].text), c))
            gen_sig = h_obj([W.fingerprint, ent.name, [(c, claims[c].content_hash) for c in order]])
            prev = pages.get(pid)
            page_plans.append((pid, ent, order, gen_sig))
            if self.forced or prev is None or prev.gen_sig != gen_sig:
                views = tuple(ClaimView(f"C{i + 1}", claims[c].text, claims[c].mentions) for i, c in enumerate(order))
                regen.append((pid, ent.name, views))

        bodies: dict[str, tuple[str, str]] = {}
        for pid, _, _ in regen:
            report.op("regenerate_page", pid)
        if self.dry_run and not W.free:
            report.exact = False
            report.planned_llm_calls["page"] = len(regen)
            for pid, _, _ in regen:
                bodies[pid] = ("(pending regeneration)", f"(pending {pid})")
        else:
            for item, res in self._map(lambda it: W.write(it[1], it[2]), regen, W.is_llm, "page"):
                report.usage.add("page", llm=W.is_llm, input_tokens=res.input_tokens, output_tokens=res.output_tokens)
                bodies[item[0]] = (res.text, first_paragraph(res.text))

        changed_pages: set[str] = set(report.ops.get("delete_page", []))
        for pid, ent, order, gen_sig in page_plans:
            prev = pages.get(pid)
            body = bodies[pid][0] if pid in bodies else prev.body  # type: ignore[union-attr]
            dropped: list[str] = []
            display = body
            if self.provenance == "strict":
                display, dropped = ground_body(body, len(order))
            lead = first_paragraph(display)
            related = self._related(ent.id, rels_by_entity, rels, entities)
            citations = []
            topic_votes: Counter[str] = Counter()
            for c in order:
                paths = sorted({sources[passages[p].source_id].path for p in claims[c].passage_ids})
                citations.append(paths)
                topic_votes.update(topic_of(p) for p in paths)
            topic = sorted(topic_votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            render_sig = h_obj([gen_sig, versions.RENDER, self.provenance, related, citations])
            rendered = self._render_page(ent.name, display, related, citations)
            page = PageArt(
                pid, ent.id, ent.name, body, lead, rendered, tuple(order),
                tuple(sorted(set(rels_by_entity.get(ent.id, ())) & rels.keys())), topic, gen_sig, render_sig, W.fingerprint,
                tuple(dropped),
            )
            if prev is None or prev.state_hash != page.state_hash:
                changed_pages.add(pid)
                if pid not in bodies:
                    report.op("rerender_page", pid)
            elif pid not in bodies:
                report.op("cutoff_page", pid)
            pages[pid] = page

        # ---------------------------------------------------------- summaries
        S = P.summarizer
        old_pages = old.of("page")
        topics: set[str] = set()
        for pid in changed_pages:
            if pid in old_pages:
                topics.add(old_pages[pid].topic)  # type: ignore[attr-defined]
            if pid in pages:
                topics.add(pages[pid].topic)
        if full or render_changed:
            topics |= {p.topic for p in pages.values()}
        topics |= {sm.topic for sm in summaries.values() if sm.generator != S.fingerprint}  # summarizer drift
        pages_by_topic: dict[str, list[PageArt]] = defaultdict(list)
        if topics:
            for p in pages.values():
                if p.topic in topics:
                    pages_by_topic[p.topic].append(p)
        sum_plans: list[tuple[str, str, list[PageArt], str]] = []
        sum_regen: list[tuple[str, str, tuple[PageLead, ...]]] = []
        for topic in sorted(topics):
            sid = summary_id_for(topic)
            members_ = sorted(pages_by_topic.get(topic, []), key=lambda p: (p.title.casefold(), p.id))
            if not members_:
                if summaries.pop(sid, None) is not None:
                    report.op("delete_summary", sid)
                continue
            sig = h_obj([S.fingerprint, topic, [(p.id, p.title, h(p.lead)) for p in members_]])
            prev_s = summaries.get(sid)
            sum_plans.append((sid, topic, members_, sig))
            if self.forced or prev_s is None or prev_s.sig != sig or not prev_s.body:
                leads = tuple(PageLead(p.title, p.id.split(":", 1)[1], p.lead) for p in members_)
                sum_regen.append((sid, topic, leads))
                report.op("regenerate_summary", sid)
            else:
                report.op("cutoff_summary", sid)
        sum_bodies: dict[str, str] = {}
        if self.dry_run and not S.free:
            report.exact = False
            report.planned_llm_calls["summary"] = len(sum_regen)
            sum_bodies = {sid: "(pending regeneration)" for sid, _, _ in sum_regen}
        else:
            for item, res in self._map(lambda it: S.summarize(it[1], it[2]), sum_regen, S.is_llm, "summary"):
                report.usage.add("summary", llm=S.is_llm, input_tokens=res.input_tokens, output_tokens=res.output_tokens)
                sum_bodies[item[0]] = res.text
        for sid, topic, members_, sig in sum_plans:
            prev_s = summaries.get(sid)
            body = sum_bodies.get(sid)
            if body is None:
                body = prev_s.body  # generation signature unchanged: reuse the generated text
            links = "\n".join(f"- [{p.title}](../pages/{p.id.split(':', 1)[1]}.md)" for p in members_)
            render_sig = h_obj([sig, versions.RENDER, links])
            if sid not in sum_bodies and prev_s is not None and prev_s.render_sig == render_sig:
                continue  # nothing to do: keep the previous version
            rendered = f"# Topic: {topic}\n\n{body}\n\n## Pages\n\n{links}\n"
            if sid not in sum_bodies:
                report.op("rerender_summary", sid)
            summaries[sid] = SummaryArt(sid, topic, f"Topic: {topic}", rendered, tuple(p.id for p in members_), sig,
                                        S.fingerprint, body, render_sig)

        # -------------------------------------------------------------- index
        page_entries = sorted(((p.title, p.id) for p in pages.values()), key=lambda t: (t[0].casefold(), t[1]))
        sum_entries = sorted(((s.title, s.id) for s in summaries.values()), key=lambda t: (t[0].casefold(), t[1]))
        index_sig = h_obj([versions.RENDER, page_entries, sum_entries])
        old_index = old.of("index").get(INDEX_ID)
        index: IndexArt | None = None
        if page_entries or sum_entries:
            if old_index is not None and not full and old_index.sig == index_sig:  # type: ignore[attr-defined]
                index = old_index  # type: ignore[assignment]
                report.op("cutoff_index", INDEX_ID)
            else:
                lines = ["# Index", "", "## Topics", ""]
                lines += [f"- [{t}](summaries/{i.split(':', 1)[1]}.md)" for t, i in sum_entries]
                lines += ["", "## Pages", ""]
                lines += [f"- [{t}](pages/{i.split(':', 1)[1]}.md)" for t, i in page_entries]
                index = IndexArt(INDEX_ID, "\n".join(lines) + "\n", tuple(i for _, i in page_entries), tuple(i for _, i in sum_entries), index_sig)
                report.op("rerender_index", INDEX_ID)

        # ---------------------------------------------------------- changeset
        cs = ChangeSet()
        for sid in sorted(set(old.sources) | set(sources)):
            before, after = old.sources.get(sid), sources.get(sid)
            if after is None:
                cs.source_deletes.append(sid)
            elif before != after:
                cs.source_upserts.append(after)

        def diff(kind: str, new: dict[str, Any], ids: Iterable[str]) -> None:
            olds = old.of(kind)
            for aid in sorted(set(ids)):
                before, after = olds.get(aid), new.get(aid)
                if after is None:
                    if before is not None:
                        cs.deletes.append((aid, kind))
                        cs.edges[aid] = []
                elif before is None or before.state_hash != after.state_hash:
                    cs.upserts.append(after)
                    if before is None or before.deps() != after.deps():
                        cs.edges[aid] = after.deps()

        all_or = (lambda kind, new, ids: set(old.of(kind)) | set(new)) if full else (lambda kind, new, ids: ids)
        diff("passage", passages, all_or("passage", passages, removed_pids | set(added)))
        diff("claim", claims, all_or("claim", claims, touched_claims))
        diff("entity", entities, all_or("entity", entities, touched_entities))
        diff("relationship", rels, all_or("relationship", rels, touched_rels))
        diff("page", pages, all_or("page", pages, candidates_page_ids(candidates)))
        diff("summary", summaries, all_or("summary", summaries, {summary_id_for(t) for t in topics}))
        diff("index", {INDEX_ID: index} if index else {}, {INDEX_ID})

        report.counts.update({
            "passages_added": len(added),
            "passages_removed": len(removed_pids),
            "claims_touched": len(touched_claims),
            "entities_touched": len(touched_entities),
            "relationships_touched": len(touched_rels),
            "pages_considered": len(page_plans),
            "summaries_considered": len(sum_plans),
            "sources_total": len(sources),
            "pages_total": len(pages),
            "claims_total": len(claims),
            "entities_total": len(entities),
        })

        new_state = KBState(sources=sources, config_fingerprint=self.config_fingerprint)
        new_state.artifacts = {
            "passage": passages, "claim": claims, "entity": entities, "relationship": rels,  # type: ignore[dict-item]
            "page": pages, "summary": summaries, "index": {INDEX_ID: index} if index else {},  # type: ignore[dict-item]
        }
        return cs, new_state

    # ============================================================== helpers
    def _map(self, fn: Callable[[T], R], items: list[T], concurrent: bool, point: str) -> list[tuple[T, R]]:
        """Run provider calls, concurrently when they are LLM calls. Results in input order."""
        out: list[tuple[T, R]] = []
        if concurrent and self.jobs > 1 and len(items) > 1:
            with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                futures = [pool.submit(fn, it) for it in items]
                for it, fut in zip(items, futures):
                    self.fault.tick(point)
                    out.append((it, fut.result()))
        else:
            for it in items:
                self.fault.tick(point)
                out.append((it, fn(it)))
        return out

    def _canonical(self, mention: str) -> str:
        return self.alias_map.get(entity_key(mention), mention)

    def _entity_ids(self, claim: ClaimArt) -> set[str]:
        return {"ent:" + entity_key(self._canonical(m)) for m in claim.mentions}

    @staticmethod
    def _pairs(eids: set[str]) -> set[str]:
        keys = sorted(e.split(":", 1)[1] for e in eids)
        return {f"rel:{a}--{b}" for i, a in enumerate(keys) for b in keys[i + 1 :]}

    @staticmethod
    def _rel_endpoints(rid: str) -> tuple[str, str]:
        a, b = rid.split(":", 1)[1].split("--", 1)
        return "ent:" + a, "ent:" + b

    def _display_name(self, eid: str, cids: set[str], claims: dict[str, ClaimArt]) -> str:
        forms: Counter[str] = Counter()
        for cid in cids:
            for m in claims[cid].mentions:
                canon = self._canonical(m)
                if "ent:" + entity_key(canon) == eid:
                    forms[canon] += 1
        return sorted(forms.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    @staticmethod
    def _related(eid: str, rels_by_entity: dict[str, set[str]], rels: dict[str, RelationshipArt], entities: dict[str, EntityArt]) -> list[list[str]]:
        scored = []
        for rid in rels_by_entity.get(eid, ()):
            r = rels.get(rid)
            if r is None:
                continue
            other = r.b if r.a == eid else r.a
            if other in entities:
                scored.append((-len(r.claim_ids), entities[other].name.casefold(), other, entities[other].name))
        scored.sort()
        return [[o, name] for _, _, o, name in scored[:RELATED_LIMIT]]

    @staticmethod
    def _render_page(name: str, body: str, related: list[list[str]], citations: list[list[str]]) -> str:
        parts = [f"# {name}", "", body.strip(), ""]
        if related:
            parts += ["## Related", ""]
            parts += [f"- [{n}]({o.split(':', 1)[1]}.md)" for o, n in related]
            parts.append("")
        parts += ["## Sources", ""]
        parts += [f"[^C{i + 1}]: {', '.join(paths)}" for i, paths in enumerate(citations)]
        return "\n".join(parts) + "\n"


def candidates_page_ids(entity_ids: Iterable[str]) -> set[str]:
    return {page_id_for(e) for e in entity_ids}
