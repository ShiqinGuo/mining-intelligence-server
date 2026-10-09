from mining_server.domain.core import Contract, DomainError, ErrorCode
from mining_server.domain.tasks import TaskExecutionDefaults, TaskState


class TaskFailureDecision(Contract):
    state: TaskState
    retry_seconds: int | None = None
    consumes_retry: bool = False


class TaskLifecycle:
    @staticmethod
    def recover(state: TaskState, uncertain: bool) -> TaskState:
        if state not in (
            TaskState.RUNNING,
            TaskState.CANCEL_REQUESTED,
            TaskState.RETRY_WAIT,
            TaskState.WAITING_QUOTA,
            TaskState.WAITING_AUTH,
        ):
            return state
        if state == TaskState.CANCEL_REQUESTED:
            return TaskState.CANCELLED
        if uncertain:
            return TaskState.UNKNOWN
        return TaskState.QUEUED

    @staticmethod
    def can_claim(state: TaskState, generation: int, expected_generation: int) -> bool:
        return state == TaskState.QUEUED and generation == expected_generation

    @staticmethod
    def can_complete(state: TaskState) -> bool:
        return state in (TaskState.SUCCEEDED, TaskState.PARTIAL)

    @staticmethod
    def can_resume(state: TaskState) -> bool:
        return state not in (
            TaskState.SUCCEEDED,
            TaskState.RUNNING,
            TaskState.QUEUED,
            TaskState.CANCEL_REQUESTED,
        )

    @staticmethod
    def cancel(state: TaskState) -> TaskState:
        match state:
            case TaskState.RUNNING | TaskState.CANCEL_REQUESTED:
                return TaskState.CANCEL_REQUESTED
            case TaskState.SUCCEEDED | TaskState.CANCELLED:
                return state
            case _:
                return TaskState.CANCELLED

    @staticmethod
    def failure(
        error: DomainError,
        retry_count: int,
        defaults: TaskExecutionDefaults,
        state: TaskState = TaskState.RUNNING,
    ) -> TaskFailureDecision:
        if state == TaskState.CANCEL_REQUESTED:
            return TaskFailureDecision(state=TaskState.CANCELLED)
        match error.code:
            case ErrorCode.BUSY:
                return TaskFailureDecision(
                    state=TaskState.RETRY_WAIT,
                    retry_seconds=error.details.retry_after
                    or defaults.busy_retry_seconds,
                )
            case ErrorCode.WAITING_AUTH:
                return TaskFailureDecision(
                    state=TaskState.WAITING_AUTH,
                    retry_seconds=defaults.auth_retry_seconds,
                )
            case ErrorCode.WAITING_QUOTA:
                return TaskFailureDecision(
                    state=TaskState.WAITING_QUOTA,
                    retry_seconds=error.details.retry_after
                    or defaults.quota_retry_seconds,
                )
            case ErrorCode.UNKNOWN_RESULT:
                return TaskFailureDecision(state=TaskState.UNKNOWN)
            case ErrorCode.CANCELLED:
                return TaskFailureDecision(state=TaskState.CANCELLED)
            case _ if (
                error.details.retryable and retry_count < defaults.failure_retry_limit
            ):
                delay = (
                    defaults.failure_retry_seconds
                    * defaults.failure_retry_multiplier**retry_count
                )
                return TaskFailureDecision(
                    state=TaskState.RETRY_WAIT,
                    retry_seconds=error.details.retry_after or delay,
                    consumes_retry=True,
                )
            case _:
                return TaskFailureDecision(state=TaskState.FAILED)
