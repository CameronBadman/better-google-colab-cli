"""Public synchronous API for the unofficial Better Colab client."""

__all__ = [
    "ArgumentSpec",
    "Artifact",
    "BatchResult",
    "BatchState",
    "BatchWaitResult",
    "BetterColabClient",
    "BetterColabError",
    "CapabilitiesResult",
    "CommandCapability",
    "CompletionSource",
    "ControllerStatus",
    "ControllerStopResult",
    "DoctorResult",
    "ErrorDetail",
    "ExecutionState",
    "ExecutionListResult",
    "ExecutionResult",
    "ExecutionSummary",
    "ExecutionTransitionSummary",
    "ExecutionWaitResult",
    "ExitCode",
    "HealthResult",
    "Limits",
    "NotebookCell",
    "NotebookCellSummary",
    "NotebookCellsResult",
    "NotebookIdsResult",
    "NotebookWriteResult",
    "OutputEvent",
    "OutputPage",
    "PruneResult",
    "SessionHealthResult",
    "SessionListResult",
    "SessionStopResult",
    "SessionSummary",
    "SourceProvenance",
]

_EXPORT_MODULES = {
    **{
        name: "better_colab.models"
        for name in __all__
        if name not in {"BetterColabClient", "BetterColabError", "ExitCode"}
    },
    "BetterColabClient": "better_colab.client",
    "BetterColabError": "better_colab.errors",
    "ExitCode": "better_colab.errors",
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
