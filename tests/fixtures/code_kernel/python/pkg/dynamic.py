def invoke(target: object, method_name: str) -> object:
    method = getattr(target, method_name)
    return method()


def invoke_shadowed(target: object) -> object:
    invoke = target
    return invoke()
