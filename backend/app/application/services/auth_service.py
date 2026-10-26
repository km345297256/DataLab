import hashlib
import secrets
import hmac
from typing import Optional
from datetime import datetime, UTC
from app.domain.models.user import User, UserRole, RegistrationStatus
from app.domain.repositories.user_repository import UserRepository
from app.application.errors.exceptions import AppException, UnauthorizedError, ValidationError, BadRequestError, ForbiddenError, NotFoundError
from app.core.config import get_settings
from app.application.services.token_service import TokenService
from app.domain.models.auth import AuthToken
from app.domain.services.token_quota_service import TokenQuotaService
from app.infrastructure.models.documents import UserDocument
import logging

logger = logging.getLogger(__name__)


class AuthService:
    """Authentication service handling user authentication and authorization"""
    
    def __init__(self, user_repository: UserRepository, token_service: TokenService):
        self.user_repository = user_repository
        self.settings = get_settings()
        self.token_service = token_service
    
    def _hash_password(self, password: str) -> str:
        """Store a per-password random salt and work factor with every new hash."""
        salt = secrets.token_hex(16)
        rounds = 600_000
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), rounds).hex()
        return f"pbkdf2_sha256${rounds}${salt}${digest}"

    def _verify_password(self, password: str, password_hash: str) -> bool:
        if not password_hash:
            return False
        try:
            if password_hash.startswith("pbkdf2_sha256$"):
                _, rounds_text, salt, expected = password_hash.split("$", 3)
                rounds = int(rounds_text)
                if not 100_000 <= rounds <= 2_000_000:
                    return False
                actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), rounds).hex()
            else:
                # Verify inherited hashes once; successful logins upgrade them.
                salt = self.settings.password_salt or ""
                rounds = self.settings.password_hash_rounds or 10
                actual = salt + hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), rounds).hex()
                expected = password_hash
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    async def bootstrap_admin(self) -> None:
        """Create only the explicitly configured initial administrator, never promote users."""
        email = (self.settings.bootstrap_admin_email or "").strip().lower()
        password = self.settings.bootstrap_admin_password or ""
        if not email and not password:
            return
        if not email or "@" not in email or len(password) < 12:
            raise RuntimeError("Bootstrap requires an email and a password of at least 12 characters")
        if await self.user_repository.get_user_by_email(email):
            return
        user = User(
            id=self._generate_user_id(), fullname="DataLab 管理员", email=email,
            password_hash=self._hash_password(password), role=UserRole.ADMIN,
            is_active=True, registration_status=RegistrationStatus.APPROVED,
            token_balance=None,
        )
        # The unique email index makes startup safe with multiple workers.
        from pymongo.errors import DuplicateKeyError
        try:
            await self.user_repository.create_user(user)
        except DuplicateKeyError:
            pass

    async def _is_revoked(self, token: str) -> bool:
        # General-purpose RedisCache intentionally hides outages; authentication
        # must distinguish unavailable revocation state from a missing key.
        from app.infrastructure.storage.redis import get_redis
        store = get_redis()
        try:
            await store.initialize()
            return bool(await store.client.exists("auth:revoked:" + hashlib.sha256(token.encode()).hexdigest()))
        except Exception:
            raise AppException(503, "Authentication is temporarily unavailable", 503) from None

    def _generate_user_id(self) -> str:
        """Generate unique user ID"""
        return secrets.token_urlsafe(16)

    async def _patch_user(self, user: User, changes: dict, *, revoke_sessions: bool = False) -> User:
        patch = getattr(self.user_repository, "patch_user", None)
        if patch is None:
            return await UserRepository.patch_user(
                self.user_repository, user.id, changes,
                expected_auth_version=user.auth_version, revoke_sessions=revoke_sessions,
            )
        return await patch(user.id, changes, expected_auth_version=user.auth_version, revoke_sessions=revoke_sessions)

    async def _require_admin(self, user_id: str) -> User:
        actor = await self.user_repository.get_user_by_id(user_id)
        if not actor or not actor.is_active or actor.registration_status != RegistrationStatus.APPROVED or actor.role != UserRole.ADMIN:
            raise ForbiddenError("Admin access required")
        return actor

    async def list_users(self, *, query=None, role=None, status=None, limit=50, offset=0):
        search = getattr(self.user_repository, "search_users", None)
        options = dict(query=query, role=role, status=status, limit=limit, offset=offset)
        if search is None:
            return await UserRepository.search_users(self.user_repository, **options)
        return await search(**options)

    def _account_management_lock(self):
        lock = getattr(self.user_repository, "account_management_lock", None)
        return lock() if lock else UserRepository.account_management_lock(self.user_repository)

    async def update_account(self, user_id: str, actor_user_id: str, *, role: UserRole | None = None, is_active: bool | None = None) -> User:
        async with self._account_management_lock():
            await self._require_admin(actor_user_id)
            user = await self.user_repository.get_user_by_id(user_id)
            if not user:
                raise NotFoundError("User not found")
            if user_id == actor_user_id and (is_active is False or (role is not None and role != UserRole.ADMIN)):
                raise BadRequestError("Cannot deactivate or demote your own administrator account")
            if user.registration_status != RegistrationStatus.APPROVED and (is_active is True or (role is not None and role != user.role)):
                raise BadRequestError("User registration must be approved before activation or role changes")
            changes = {}
            if role is not None and role != user.role:
                changes["role"] = role
            if is_active is not None and is_active != user.is_active:
                changes["is_active"] = is_active
            if not changes:
                return user
            return await self._patch_user(user, changes, revoke_sessions=True)
    
    async def register_user(self, fullname: str, password: str, email: str, role: UserRole = UserRole.USER) -> User:
        """Register a new user"""
        logger.info(f"Registering user: {email}")

        if self.settings.auth_provider != "password":
            raise BadRequestError("Registration is not allowed")
        
        # Validate input
        if not fullname or len(fullname.strip()) < 2:
            raise ValidationError("Full name must be at least 2 characters long")
        
        if not email or '@' not in email:
            raise ValidationError("Valid email is required")
        
        if not password or len(password) < 6:
            raise ValidationError("Password must be at least 6 characters long")
        
        email = email.strip().lower()
        # Self-service registration cannot choose an elevated role.
        if role != UserRole.USER:
            raise ForbiddenError("Registration can only create a regular user")
        # Check if email already exists
        if await self.user_repository.email_exists(email):
            raise ValidationError("Email already exists")
        
        # Hash password
        password_hash = self._hash_password(password)
        
        # Create user
        user = User(
            id=self._generate_user_id(),
            fullname=fullname.strip(),
            email=email.lower(),
            password_hash=password_hash,
            role=role,
            is_active=False,
            registration_status=RegistrationStatus.PENDING,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        # Save to database
        from pymongo.errors import DuplicateKeyError
        try:
            created_user = await self.user_repository.create_user(user)
        except DuplicateKeyError:
            raise ValidationError("Email already exists") from None
        user_doc = await UserDocument.find_one({"user_id": created_user.id})
        if user_doc:
            await TokenQuotaService().initialize_user_quota(user_doc)
            created_user = user_doc.to_domain()
        
        logger.info(f"User registered successfully: {created_user.id}")
        return created_user

    async def verify_registered_user(self, user_id: str, email: str) -> User:
        """Validate a legacy verification token without bypassing approval."""
        user = await self.user_repository.get_user_by_id(user_id)
        if not user or user.email != email.lower():
            raise UnauthorizedError("Invalid or expired verification link")
        return user

    async def decide_registration(
        self,
        user_id: str,
        status: RegistrationStatus,
        reviewer_user_id: str,
        note: Optional[str] = None,
    ) -> User:
        """Approve or reject a pending registration request."""
        if status not in {RegistrationStatus.APPROVED, RegistrationStatus.REJECTED}:
            raise ValidationError("Registration decision must be approved or rejected")

        async with self._account_management_lock():
            await self._require_admin(reviewer_user_id)
            user = await self.user_repository.get_user_by_id(user_id)
            if not user:
                raise ValidationError("User not found")
            if user.registration_status != RegistrationStatus.PENDING:
                raise BadRequestError("Registration request has already been reviewed")

            return await self._patch_user(user, {
                "registration_status": status,
                "is_active": status == RegistrationStatus.APPROVED,
                "registration_reviewed_by": reviewer_user_id,
                "registration_reviewed_at": datetime.now(UTC),
                "registration_review_note": (note or "").strip() or None,
            }, revoke_sessions=True)
    
    async def authenticate_user(self, email: str, password: str) -> Optional[User]:
        """Authenticate user by email and password"""
        logger.debug(f"Authenticating user: {email}")
        
        if self.settings.auth_provider != "password":
            raise BadRequestError("DataLab requires password authentication")
        if self.settings.auth_provider == "password":
            # Database password authentication
            user = await self.user_repository.get_user_by_email(email)
            if not user:
                logger.warning(f"User not found: {email}")
                return None
            
            if not user.password_hash:
                logger.warning(f"User has no password hash: {email}")
                return None
            
            # Verify password
            if not self._verify_password(password, user.password_hash):
                logger.warning(f"Invalid password for user: {email}")
                return None

            if user.registration_status == RegistrationStatus.PENDING:
                raise ForbiddenError("Registration is pending administrator approval")
            if user.registration_status == RegistrationStatus.REJECTED:
                raise ForbiddenError("Registration request was rejected by an administrator")
            if not user.is_active:
                logger.warning(f"User account is inactive: {email}")
                raise ForbiddenError("User account is inactive")
            
            changes = {"last_login_at": datetime.now(UTC)}
            legacy_hash = not user.password_hash.startswith("pbkdf2_sha256$")
            if legacy_hash:
                changes["password_hash"] = self._hash_password(password)
            # Targeted CAS cannot restore a role/status changed during hashing.
            user = await self._patch_user(user, changes, revoke_sessions=legacy_hash)
            
            logger.info(f"User authenticated successfully: {email}")
            return user
        
        else:
            raise ValueError(f"Unsupported auth provider: {self.settings.auth_provider}")
    
    async def login_with_tokens(self, email: str, password: str) -> AuthToken:
        """Authenticate user and return JWT tokens"""
        user = await self.authenticate_user(email, password)
        
        if not user:
            raise UnauthorizedError("Invalid email or password")
        
        # Generate JWT tokens
        access_token = self.token_service.create_access_token(user)
        refresh_token = self.token_service.create_refresh_token(user)
        
        return AuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=user
        )
    
    async def refresh_access_token(self, refresh_token: str) -> AuthToken:
        """Refresh access token using refresh token"""
        payload = self.token_service.verify_token(refresh_token)
        
        if not payload or await self._is_revoked(refresh_token):
            raise UnauthorizedError("Invalid refresh token")
        
        if payload.get("type") != "refresh":
            raise UnauthorizedError("Invalid token type")
        
        # Get user from database
        user_id = payload.get("sub")
        user = await self.user_repository.get_user_by_id(user_id)
        
        if not user or not user.is_active or user.registration_status != RegistrationStatus.APPROVED:
            raise UnauthorizedError("User not found or inactive")
        
        if payload.get("password_version") != hashlib.sha256((user.password_hash or "").encode()).hexdigest():
            raise UnauthorizedError("Password changed; please log in again")
        if payload.get("auth_version", 0) != user.auth_version:
            raise UnauthorizedError("Account changed; please log in again")

        # Generate new access token
        new_access_token = self.token_service.create_access_token(user)
        
        return AuthToken(
            access_token=new_access_token,
            token_type="bearer"
        )
    
    async def verify_token(self, token: str) -> Optional[User]:
        user_info = self.token_service.get_user_from_token(token)
        if not user_info or self.settings.auth_provider != "password" or await self._is_revoked(token):
            return None
        user = await self.user_repository.get_user_by_id(user_info["id"])
        if not user or not user.is_active or user.registration_status != RegistrationStatus.APPROVED:
            return None
        if user_info.get("password_version") != hashlib.sha256((user.password_hash or "").encode()).hexdigest():
            return None
        if user_info.get("auth_version", 0) != user.auth_version:
            return None
        return user

    async def logout(self, token: str, refresh_token: str | None = None) -> bool:
        """Revoke this browser's access and refresh credentials until their expiry."""
        from app.infrastructure.storage.redis import get_redis
        store = get_redis()
        access = self.token_service.verify_token(token)
        for candidate in (token, refresh_token):
            if not candidate:
                continue
            payload = self.token_service.verify_token(candidate)
            if not payload or not access or payload.get("sub") != access.get("sub"):
                continue
            ttl = max(1, int(payload["exp"] - datetime.now(UTC).timestamp()))
            try:
                await store.initialize()
                saved = await store.client.setex("auth:revoked:" + hashlib.sha256(candidate.encode()).hexdigest(), ttl, "1")
                if not saved:
                    raise RuntimeError("Revocation was not persisted")
            except Exception:
                raise AppException(503, "Authentication is temporarily unavailable", 503) from None
        return True

    async def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        """Change user password"""
        logger.info(f"Changing password for user: {user_id}")
        
        # Get user
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        
        if not user.is_active or user.registration_status != RegistrationStatus.APPROVED:
            raise UnauthorizedError("User account is inactive")
        
        # Verify old password
        if not user.password_hash or not self._verify_password(old_password, user.password_hash):
            raise UnauthorizedError("Invalid old password")
        
        # Validate new password
        if not new_password or len(new_password) < 6:
            raise ValidationError("New password must be at least 6 characters long")
        
        # Hash new password
        new_password_hash = self._hash_password(new_password)
        
        # Update user password
        await self._patch_user(user, {"password_hash": new_password_hash}, revoke_sessions=True)
        
        logger.info(f"Password changed successfully for user: {user_id}")
        return True
    
    async def change_fullname(self, user_id: str, new_fullname: str) -> User:
        """Change user fullname"""
        logger.info(f"Changing fullname for user: {user_id}")
        
        # Get user
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        
        if not user.is_active or user.registration_status != RegistrationStatus.APPROVED:
            raise UnauthorizedError("User account is inactive")
        
        # Validate new fullname
        if not new_fullname or len(new_fullname.strip()) < 2:
            raise ValidationError("Full name must be at least 2 characters long")
        
        # Update user fullname
        updated_user = await self._patch_user(user, {"fullname": new_fullname.strip()})
        
        logger.info(f"Fullname changed successfully for user: {user_id}")
        return updated_user
    
    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Get user by ID"""
        return await self.user_repository.get_user_by_id(user_id)
    
    async def deactivate_user(self, user_id: str, actor_user_id: str) -> bool:
        """Deactivate user account"""
        logger.info(f"Deactivating user: {user_id}")
        
        await self.update_account(user_id, actor_user_id, is_active=False)
        
        logger.info(f"User deactivated successfully: {user_id}")
        return True
    
    async def activate_user(self, user_id: str, actor_user_id: str) -> bool:
        """Activate user account"""
        logger.info(f"Activating user: {user_id}")
        
        await self.update_account(user_id, actor_user_id, is_active=True)
        
        logger.info(f"User activated successfully: {user_id}")
        return True
    
    async def reset_password(self, email: str, new_password: str) -> bool:
        """Reset user password with email"""
        logger.info(f"Resetting password for user: {email}")
        
        if self.settings.auth_provider != "password":
            raise BadRequestError("Password reset is not allowed")
        
        # Get user by email
        user = await self.user_repository.get_user_by_email(email)
        if not user:
            raise ValidationError("User not found")
        
        if not user.is_active or user.registration_status != RegistrationStatus.APPROVED:
            raise UnauthorizedError("User account is inactive")
        
        # Validate new password
        if not new_password or len(new_password) < 6:
            raise ValidationError("New password must be at least 6 characters long")
        
        # Hash new password
        new_password_hash = self._hash_password(new_password)
        
        # Update user password
        await self._patch_user(user, {"password_hash": new_password_hash}, revoke_sessions=True)
        
        logger.info(f"Password reset successfully for user: {email}")
        return True 
