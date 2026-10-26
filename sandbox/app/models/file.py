"""
File operation related models
"""
from pydantic import BaseModel, Field
from typing import List, Optional


class FileReadResult(BaseModel):
    """File read result"""
    content: str = Field(..., description="File content")
    file: str = Field(..., description="Path of the read file")
    file_sha256: Optional[str] = None
    sha256_scope: Optional[str] = None
    file_size_bytes: Optional[int] = None
    content_scope: Optional[str] = None
    complete_content: Optional[bool] = None
    version_available: bool = False


class FilePatchResult(BaseModel):
    file: str
    before_sha256: str
    after_sha256: str
    replaced_count: int
    replacement_counts: List[int]
    changed: bool
    write_action: str
    side_effect_state: str
    syntax_check: str


class FileWriteResult(BaseModel):
    """File write result"""
    file: str = Field(..., description="Path of the written file")
    bytes_written: Optional[int] = Field(None, description="Number of bytes written")
    changed: Optional[bool] = None
    write_action: Optional[str] = None
    side_effect_state: Optional[str] = None


class FileReplaceResult(BaseModel):
    """File content replacement result"""
    file: str = Field(..., description="Path of the operated file")
    replaced_count: int = Field(0, description="Number of replacements")
    changed: Optional[bool] = None
    write_action: Optional[str] = None
    side_effect_state: Optional[str] = None


class FileSearchResult(BaseModel):
    """File content search result"""
    file: str = Field(..., description="Path of the searched file")
    matches: List[str] = Field([], description="List of matched content")
    line_numbers: List[int] = Field([], description="List of matched line numbers")


class FileFindResult(BaseModel):
    """File find result"""
    path: str = Field(..., description="Path of the search directory")
    files: List[str] = Field([], description="List of found files")


class FileUploadResult(BaseModel):
    """File upload result"""
    file_path: str = Field(..., description="Path of the uploaded file")
    file_size: int = Field(..., description="Size of the uploaded file in bytes")
    success: bool = Field(..., description="Whether upload was successful")
