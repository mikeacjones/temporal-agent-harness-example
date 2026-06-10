from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, Callable, get_type_hints


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def bind_keyword_arguments(
    fn: Callable[..., Any],
    args: Mapping[str, Any] | None = None,
    *,
    special_values: Mapping[Any, Any] | None = None,
    missing_special_errors: Mapping[Any, str] | None = None,
    argument_label: str,
    reject_unexpected: bool = True,
) -> dict[str, Any]:
    """Build kwargs for a user-defined harness callback.

    Tools, guards, and routed activity functions all follow the same calling
    convention: framework context objects are injected by type annotation, and
    model/provider-supplied values are matched by parameter name.
    """

    provided_args = dict(args or {})
    injected = dict(special_values or {})
    missing_injected = dict(missing_special_errors or {})
    signature = inspect.signature(fn)
    type_hints = get_type_hints(fn)
    kwargs: dict[str, Any] = {}
    consumed_args: set[str] = set()

    for name, parameter in signature.parameters.items():
        if name in ("self", "cls"):
            continue

        annotation = type_hints.get(name, parameter.annotation)
        if annotation in injected:
            kwargs[name] = injected[annotation]
            continue
        if annotation in missing_injected:
            raise TypeError(
                missing_injected[annotation].format(
                    function=fn.__name__,
                    parameter=name,
                )
            )

        if name in provided_args:
            kwargs[name] = provided_args[name]
            consumed_args.add(name)
            continue

        if parameter.default is inspect.Parameter.empty:
            raise TypeError(
                f"Missing required {argument_label} argument {fn.__name__}.{name}"
            )

    if reject_unexpected:
        unexpected_args = set(provided_args) - consumed_args
        if unexpected_args:
            names = ", ".join(sorted(unexpected_args))
            raise TypeError(
                f"Unexpected {argument_label} argument(s) for {fn.__name__}: {names}"
            )

    return kwargs
