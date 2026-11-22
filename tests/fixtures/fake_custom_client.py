"""A bring-your-own client used by tests of the `custom` provider."""
import json

from dakc.providers.llm import Completion


class EchoClient:
    def __init__(self, model: str = "echo"):
        self.fingerprint = f"echo:{model}"
        self.calls = 0

    def complete(self, system: str, user: str, json_schema=None) -> Completion:
        self.calls += 1
        if "extract atomic factual claims" in system:
            lines = user.split("<<<\n", 1)[1].split("\n>>>", 1)[0].splitlines()
            claims = []
            for line in lines:
                n, text = line[1:].split("] ", 1)
                claims.append({"text": text, "entities": [w.strip(".,") for w in text.split() if w[:1].isupper()], "sentences": [int(n)]})
            return Completion(json.dumps({"claims": claims}), 10, 5)
        return Completion("Lead paragraph [^C1].", 10, 5)


def make(model: str = "echo") -> EchoClient:
    return EchoClient(model)
