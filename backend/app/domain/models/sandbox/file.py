"""
File operation related models
"""
from pydantic import BaseModel, ConfigDict, Field
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


class FilePatchReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    old_str: str = Field(min_length=1, max_length=8 * 1024 * 1024)
    new_str: str = Field(max_length=8 * 1024 * 1024)
    expected_count: int = Field(ge=1, le=100000)


class FileWriteResult(BaseModel):
    """File write result"""
    file: str = Field(..., description="Path of the written file")
    bytes_written: Optional[int] = Field(None, description="Number of bytes written")


class FileReplaceResult(BaseModel):
    """File content replacement result"""
    file: str = Field(..., description="Path of the operated file")
    replaced_count: int = Field(0, description="Number of replacements")


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
