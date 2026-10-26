from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, List, Optional
from app.domain.models.user import User


class UserRepository(ABC):
    """User repository interface"""
    
    @abstractmethod
    async def create_user(self, user: User) -> User:
        """Create a new user"""
        pass
    
    @abstractmethod
    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Get user by ID"""
        pass
    
    @abstractmethod
    async def get_user_by_fullname(self, fullname: str) -> Optional[User]:
        """Get user by fullname"""
        pass
    
    @abstractmethod
    async def get_user_by_email(self, email: str) -> Optional[User]:
        """Get user by email"""
        pass
    
    @abstractmethod
    async def update_user(self, user: User) -> User:
        """Update user information"""
        pass
    
    @abstractmethod
    async def delete_user(self, user_id: str) -> bool:
        """Delete user by ID"""
        pass
    
    @abstractmethod
    async def list_users(self, limit: int = 100, offset: int = 0) -> List[User]:
        """List users with pagination"""
        pass
    
    @abstractmethod
    async def fullname_exists(self, fullname: str) -> bool:
        """Check if fullname exists"""
        pass
    
    @abstractmethod
    async def email_exists(self, email: str) -> bool:
        """Check if email exists"""
        pass

    async def patch_user(
        self, user_id: str, changes: dict[str, Any], *,
        expected_auth_version: int | None = None, revoke_sessions: bool = False,
    ) -> User:
        """Fallback for non-persistent repositories; database adapters use atomic CAS."""
        from app.application.errors.exceptions import ConflictError, NotFoundError
        user = await self.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("User not found")
        if expected_auth_version is not None and user.auth_version != expected_auth_version:
            raise ConflictError("Account changed; refresh and try again")
        for key, value in changes.items():
            setattr(user, key, value)
        if revoke_sessions:
            user.auth_version += 1
        user.updated_at = datetime.now(UTC)
        return await self.update_user(user)

    @asynccontextmanager
    async def account_management_lock(self):
        """Persistent adapters serialize administrator changes across workers."""
        yield

    async def search_users(self, *, query=None, role=None, status=None, limit=50, offset=0):
        users = await self.list_users(limit=1_000_000, offset=0)
        if query:
            needle = query.strip().lower()
            users = [user for user in users if any(needle in value.lower() for value in (user.id, user.fullname, user.email))]
        if role:
            users = [user for user in users if user.role == role]
        if status in {"active", "inactive"}:
            users = [user for user in users if user.registration_status == "approved" and user.is_active == (status == "active")]
        elif status:
            users = [user for user in users if user.registration_status == status]
        users.sort(key=lambda user: (user.created_at.timestamp(), user.id), reverse=True)
        return users[offset:offset + limit], len(users)
