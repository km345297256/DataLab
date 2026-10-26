"""Keep saved research data when chats replace artifacts or remove attachments."""
from app.application.errors.exceptions import AppException


class RetainedFileError(AppException):
    def __init__(self):
        super().__init__(409, "文件已保存到我的数据，请先移除对应的数据条目后再删除文件", 409)


class RetainedFileStorage:
    def __init__(self, storage, repository):
        self.storage = storage
        self.repository = repository

    def __getattr__(self, name):
        return getattr(self.storage, name)

    async def delete_file(self, file_id: str, user_id: str) -> bool:
        info = await self.storage.get_file_info(file_id, user_id)
        if info is None or info.user_id != user_id:
            return False
        if not await self.repository.begin_file_delete(user_id, file_id):
            raise RetainedFileError()
        deleted = False
        try:
            deleted = await self.storage.delete_file(file_id, user_id)
            return deleted
        finally:
            await self.repository.finish_file_delete(user_id, file_id, deleted=deleted)
