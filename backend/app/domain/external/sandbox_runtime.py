from typing import Optional, Protocol, Sequence

from app.domain.external.sandbox import Sandbox
from app.domain.models.session import Session
from app.domain.models.file import FileInfo


class SandboxNotFoundError(RuntimeError):
    """The persisted sandbox id no longer exists on its execution node."""


class SandboxRuntime(Protocol):
    async def allocate(self, session: Optional[Session] = None, dataset_ids: Sequence[str] | None = None,
                       input_files: Sequence[FileInfo] | None = None) -> Sandbox:
        """Allocate a sandbox for a session or warm pool."""
        ...

    async def restore(self, sandbox_id: str) -> Sandbox:
        """Restore a sandbox handle from a persisted sandbox id."""
        ...

    async def assign(self, sandbox: Sandbox, session: Session, task_id: Optional[str] = None) -> None:
        """Associate an allocated sandbox with a session/task for observability."""
        ...
