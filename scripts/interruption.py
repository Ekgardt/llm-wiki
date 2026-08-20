"""Interruption-aware error propagation shared by the LSP modules.

A cleanup that fails while a KeyboardInterrupt or SystemExit is travelling
must not bury the interruption under an ordinary error, and must not hand
back an exception whose cause or context points at itself. The three LSP
modules each grew their own copy of these rules; this is the one copy.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence


def _chained(current: BaseException) -> tuple[BaseException, ...]:
    """The exceptions this one points at, context and cause alike."""
    linked = (current.__context__, current.__cause__)
    return tuple(item for item in linked if item is not None)


def walk_exception_chain(error: BaseException | None) -> Iterator[BaseException]:
    """Every exception reachable from this one, each visited exactly once."""
    pending = [error] if error is not None else []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend(_chained(current))


def interruption_in_chain(
    error: BaseException,
) -> KeyboardInterrupt | SystemExit | None:
    """The interruption anywhere in the error's cause and context chain."""
    for current in walk_exception_chain(error):
        if isinstance(current, (KeyboardInterrupt, SystemExit)):
            return current
    return None


def exception_reaches(error: BaseException | None, target: BaseException) -> bool:
    """Whether the target is reachable from the error by cause or context."""
    return any(current is target for current in walk_exception_chain(error))


def _first_interruption(
    ordered: tuple[BaseException, ...],
) -> tuple[BaseException | None, BaseException | None]:
    """The first interruption among the errors, and the error carrying it."""
    for error in ordered:
        interruption = interruption_in_chain(error)
        if interruption is not None:
            return interruption, error
    return None, None


def _other_error(
    ordered: tuple[BaseException, ...],
    source: BaseException | None,
    interruption: BaseException,
) -> BaseException | None:
    """The first error that is neither the interruption nor its carrier."""
    for error in ordered:
        if error is not source and error is not interruption:
            return error
    return None


def _detached_secondary(
    secondary: BaseException, interruption: BaseException
) -> BaseException | None:
    """The secondary error with any chain back to the interruption cut."""
    if exception_reaches(secondary.__cause__, interruption):
        secondary.__cause__ = None
    if exception_reaches(secondary.__context__, interruption):
        secondary.__context__ = None
    if exception_reaches(secondary, interruption):
        return None
    return secondary


def _secondary_error(
    ordered: tuple[BaseException, ...],
    source: BaseException | None,
    interruption: BaseException,
) -> BaseException | None:
    """The error worth reporting alongside an interruption, if any."""
    secondary = _other_error(ordered, source, interruption)
    if secondary is None and source is not interruption:
        secondary = source
    if secondary is None:
        return None
    return _detached_secondary(secondary, interruption)


def _cut_self_reference(interruption: BaseException) -> None:
    """An exception must not end up as its own cause or context."""
    if exception_reaches(interruption.__cause__, interruption):
        interruption.__cause__ = None
    if exception_reaches(interruption.__context__, interruption):
        interruption.__context__ = None
    if interruption.__context__ is interruption.__cause__:
        interruption.__context__ = None


def _raise_interruption(
    interruption: BaseException, secondary: BaseException | None
) -> None:
    """Raise the interruption, reporting the secondary error without a loop."""
    try:
        if secondary is not None:
            raise interruption.with_traceback(
                interruption.__traceback__
            ) from secondary
        raise interruption.with_traceback(interruption.__traceback__)
    except (KeyboardInterrupt, SystemExit) as raised:
        if raised is not interruption:
            raise
        _cut_self_reference(interruption)
        raise


def _raise_first_error(
    errors: Sequence[BaseException],
    prior_error: BaseException | None,
) -> None:
    """Raise what was collected, chaining the prior error when there is one."""
    if errors:
        if prior_error is not None:
            raise errors[0] from prior_error
        raise errors[0]
    if prior_error is not None:
        raise prior_error


def raise_collected_errors(
    errors: Sequence[BaseException],
    *,
    prior_error: BaseException | None = None,
) -> None:
    """Raise the interruption if one is travelling, otherwise the first error."""
    ordered = ((prior_error,) if prior_error is not None else ()) + tuple(errors)
    interruption, source = _first_interruption(ordered)
    if interruption is not None:
        _raise_interruption(
            interruption, _secondary_error(ordered, source, interruption)
        )
    _raise_first_error(errors, prior_error)
