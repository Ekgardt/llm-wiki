"""Helpers shared by test_critic_sizing_owns_immutable_native_parsing.py and the test files importing them."""




def _observe_parse(module, name, monkeypatch):
    calls = []
    original = getattr(module, name)

    def observed(content):
        calls.append(content)
        return original(content)

    monkeypatch.setattr(module, name, observed)
    return calls
