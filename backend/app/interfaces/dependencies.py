import logging
from functools import lru_cache
from pathlib import Path
from fastapi import Depends, Request, WebSocket, WebSocketException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.application.errors.exceptions import UnauthorizedError

from app.core.config import get_settings
from app.domain.external.plugin_runtime import PluginRuntime
from app.infrastructure.external.file.factory import get_file_storage
from app.infrastructure.external.file.spill_factory import get_spill_artifact_store
from app.infrastructure.external.analysis_job_factory import get_analysis_job_service
from app.infrastructure.external.tool_approval_factory import get_tool_approval_service
from app.infrastructure.external.credential_factory import get_credential_service
from app.infrastructure.external.plugins import (
    NodePluginRuntime,
    default_execution_contract_directory,
    default_plugin_host_path,
    default_tool_plugins_directory,
)
from app.infrastructure.external.search import get_search_engine
from app.domain.models.user import User, UserRole

# Import all required services
from app.application.services.agent_service import AgentService
from app.application.services.file_service import FileService
from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.application.services.email_service import EmailService
from app.infrastructure.external.cache import get_cache

# Import all required dependencies for agent service
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.task.redis_task import RedisStreamTask
from app.infrastructure.repositories.mongo_agent_repository import MongoAgentRepository
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
from app.infrastructure.repositories.mongo_mcp_repository import MongoMCPRepository
from app.infrastructure.repositories.user_repository import MongoUserRepository
from app.application.services.api_key_service import APIKeyService
from app.infrastructure.repositories.api_key_repository import MongoAPIKeyRepository
from app.application.services.agent_profile_service import AgentProfileService
from app.application.services.jupyter_service import JupyterService
from app.application.services.visualization_catalog import VisualizationCatalogService
from app.infrastructure.repositories.mongo_visualization_repository import MongoVisualizationRepository
from app.infrastructure.repositories.agent_profile_repository import MongoAgentProfileRepository


# Configure logging
logger = logging.getLogger(__name__)


@lru_cache()
def get_plugin_runtime() -> PluginRuntime | None:
    """Return the process-wide Cordis supervisor, or None for explicit rollback."""
    settings = get_settings()
    if not settings.plugin_runtime_enabled:
        return None

    host_path = (
        Path(settings.plugin_runtime_host_path)
        if settings.plugin_runtime_host_path.strip()
        else default_plugin_host_path()
    )
    tools_dir = (
        Path(settings.plugin_runtime_tools_dir)
        if settings.plugin_runtime_tools_dir.strip()
        else default_tool_plugins_directory()
    )
    execution_contract_dir = (
        Path(settings.plugin_runtime_execution_contract_dir)
        if settings.plugin_runtime_execution_contract_dir.strip()
        else default_execution_contract_directory()
    )
    return NodePluginRuntime(
        host_path=host_path,
        tools_dir=tools_dir,
        execution_contract_dir=execution_contract_dir,
        node_executable=settings.plugin_runtime_node_executable,
        request_timeout_seconds=settings.plugin_runtime_request_timeout_seconds,
        startup_timeout_seconds=settings.plugin_runtime_startup_timeout_seconds,
        shutdown_timeout_seconds=settings.plugin_runtime_shutdown_timeout_seconds,
        max_response_frame_bytes=settings.plugin_runtime_max_response_frame_bytes,
    )

@lru_cache()
def get_visualization_catalog() -> VisualizationCatalogService:
    return VisualizationCatalogService(get_plugin_runtime(), MongoVisualizationRepository())


@lru_cache()
def get_agent_service() -> AgentService:
    """
    Get agent service instance with all required dependencies
    
    This function creates and returns an AgentService instance with all
    necessary dependencies. Uses lru_cache for singleton pattern.
    """
    logger.info("Creating AgentService instance")
    
    # Create all dependencies
    agent_repository = MongoAgentRepository()
    session_repository = MongoSessionRepository()
    from app.infrastructure.repositories.mongo_input_repository import MongoInputRepository
    sandbox_cls = DockerSandbox
    task_cls = RedisStreamTask
    file_storage = get_file_storage()
    search_engine = get_search_engine()
    mcp_repository = MongoMCPRepository()
    
    # Create AgentService instance
    return AgentService(
        agent_repository=agent_repository,
        session_repository=session_repository,
        sandbox_cls=sandbox_cls,
        task_cls=task_cls,
        file_storage=file_storage,
        search_engine=search_engine,
        mcp_repository=mcp_repository,
        plugin_runtime=get_plugin_runtime(),
        spill_artifact_store=get_spill_artifact_store(),
        analysis_job_service=get_analysis_job_service(),
        tool_approval_service=get_tool_approval_service(),
        credential_service=get_credential_service(),
        input_repository=MongoInputRepository(session_repository),
    )


@lru_cache()
def get_file_service() -> FileService:
    """
    Get file service instance with required dependencies
    
    This function creates and returns a FileService instance with
    the necessary file storage and token service dependencies.
    """
    logger.info("Creating FileService instance")
    
    # Get dependencies
    file_storage = get_file_storage()
    token_service = get_token_service()
    
    return FileService(
        file_storage=file_storage,
        token_service=token_service,
    )


@lru_cache()
def get_auth_service() -> AuthService:
    """
    Get authentication service instance with required dependencies
    
    This function creates and returns an AuthService instance with
    the necessary user repository dependency.
    """
    logger.info("Creating AuthService instance")
    
    # Get user repository dependency
    user_repository = MongoUserRepository()
    
    return AuthService(
        user_repository=user_repository,
        token_service=get_token_service(),
    )


def get_user_repository() -> MongoUserRepository:
    return MongoUserRepository()


@lru_cache()
def get_token_service() -> TokenService:
    """Get token service instance"""
    logger.info("Creating TokenService instance")
    return TokenService()


@lru_cache()
def get_email_service() -> EmailService:
    """Get email service instance"""
    logger.info("Creating EmailService instance")
    cache = get_cache()
    return EmailService(cache=cache)


@lru_cache()
def get_api_key_service() -> APIKeyService:
    """Get API key service instance"""
    logger.info("Creating APIKeyService instance")
    api_key_repository = MongoAPIKeyRepository()
    user_repository = MongoUserRepository()
    return APIKeyService(api_key_repository=api_key_repository, user_repository=user_repository)


@lru_cache()
def get_agent_profile_service() -> AgentProfileService:
    """Get agent profile service instance"""
    logger.info("Creating AgentProfileService instance")
    return AgentProfileService(repository=MongoAgentProfileRepository())


@lru_cache()
def get_jupyter_service() -> JupyterService:
    return JupyterService()


_bearer = HTTPBearer(auto_error=False)


async def get_optional_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    auth_service: AuthService = Depends(get_auth_service),
) -> User | None:
    """A missing identity is allowed only at explicitly public call sites."""
    if credentials is None:
        return None
    user = await auth_service.verify_token(credentials.credentials)
    if user is None:
        raise UnauthorizedError("Invalid or expired access token")
    return user


async def get_current_user(
    user: User | None = Depends(get_optional_current_user),
) -> User:
    if user is None:
        raise UnauthorizedError("Please log in to continue")
    return user


async def verify_signature(
    request: Request,
    token_service: TokenService = Depends(get_token_service),
) -> str:
    """A signed URL grants temporary access to exactly its path and query."""
    if not token_service.verify_signed_url(str(request.url)):
        raise UnauthorizedError("Invalid or expired resource link")
    return request.query_params["signature"]


async def verify_signature_websocket(
    websocket: WebSocket,
    token_service: TokenService = Depends(get_token_service),
) -> str:
    if not token_service.verify_signed_url(str(websocket.url)):
        raise WebSocketException(code=1008, reason="Invalid or expired resource link")
    return websocket.query_params["signature"]
