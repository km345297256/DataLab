from typing import Annotated, Literal, Optional, Dict, Any
from pydantic import AfterValidator, Field
from app.domain.external.sandbox import Sandbox
from app.domain.services.tools.base import BaseToolkit, tool_execution_contract
from app.domain.models.tool_result import ToolResult
from langchain.tools import tool
from app.domain.models.sandbox.file import FilePatchReplacement


def _require_nonblank_file_target(value: str) -> str:
    if not value.strip():
        raise ValueError("File target must not be blank")
    # Whitespace may be part of a valid filename. Validate without choosing or
    # normalizing the model's target; dispatch must preserve the original value.
    return value


_FileWriteTarget = Annotated[
    str, Field(min_length=1), AfterValidator(_require_nonblank_file_target)
]


class FileToolkit(BaseToolkit):
    """File tool class, providing file operation functions"""

    name: str = "file"
    
    def __init__(self, sandbox: Sandbox):
        """Initialize file tool class
        
        Args:
            sandbox: Sandbox service
        """
        super().__init__()
        self.sandbox = sandbox
        
    @tool_execution_contract(effects=("sandbox_read",))
    @tool(parse_docstring=True)
    async def file_read(
        self,
        file: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        sudo: Literal[False] = False,
        include_version: bool = False,
    ) -> ToolResult:
        """Read file content. Use for checking file contents, analyzing logs, or reading configuration files.
        
        Args:
            file: Absolute path of the file to read
            start_line: (Optional) Starting line to read from, 0-based
            end_line: (Optional) Ending line number (exclusive)
            sudo: Must remain false; core read-only tools do not support privilege elevation
            include_version: Read a bounded complete file snapshot for file_sha256 before file_apply_patch; a partial displayed excerpt is not the hashed file
        """
        # Directly call sandbox's file_read method
        version = {"include_version": True} if include_version else {}
        return await self.sandbox.file_read(
            file=file,
            start_line=start_line,
            end_line=end_line,
            sudo=sudo,
            **version,
        )

    @tool_execution_contract(effects=("sandbox_write",))
    @tool(parse_docstring=True)
    async def file_apply_patch(
        self,
        file: _FileWriteTarget,
        base_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        replacements: Annotated[list[FilePatchReplacement], Field(min_length=1, max_length=64)],
        syntax_check: Literal["none", "python", "json"] = "none",
    ) -> ToolResult:
        """Apply all verified corrections to one file as a version-checked transaction.

        First call file_read with include_version=true. Use its full-file
        file_sha256, never a digest of a displayed excerpt. All replacement
        targets match the ORIGINAL file, not earlier replacement output.
        Wrong counts, overlaps, a stale base or invalid requested syntax save
        nothing. Identical final bytes reuse the current version. Syntax checks
        parse only; they do not execute code or prove the analysis is correct.

        Args:
            file: Absolute path of the existing writable regular file; no symlinks or analysis inputs
            base_sha256: Full-file SHA256 from a versioned read of this file
            replacements: Exact nonempty old_str/new_str replacements, each with a positive expected_count in the original file
            syntax_check: Optional candidate syntax validation: none, python (parse and compile without executing), or strict json
        """
        return await self.sandbox.file_apply_patch(
            file=file, base_sha256=base_sha256,
            replacements=[item.model_dump() for item in replacements], syntax_check=syntax_check,
        )
    
    @tool(parse_docstring=True)
    async def file_write(
        self,
        file: _FileWriteTarget,
        content: str,
        append: Optional[bool] = False,
        leading_newline: Optional[bool] = False,
        trailing_newline: Optional[bool] = False,
        sudo: Optional[bool] = False
    ) -> ToolResult:
        """Write a complete draft once, or append a distinct section to a file.

        After a successful save, run a program using its supported runtime or
        deliver a report. Do not rewrite the whole file just to rephrase it.
        Prefer file_apply_patch with a versioned read for related corrections
        justified by input or runtime feedback. A saved script is not evidence
        that its outputs were generated.
        Identical content reuses the saved version without another disk write.
        
        Args:
            file: Absolute path of the file to write to
            content: Text content to write
            append: (Optional) Whether to use append mode
            leading_newline: (Optional) Whether to add a leading newline
            trailing_newline: (Optional) Whether to add a trailing newline
            sudo: (Optional) Whether to use sudo privileges
        """
        # Prepare content
        final_content = content
        if leading_newline:
            final_content = "\n" + final_content
        if trailing_newline:
            final_content = final_content + "\n"
            
        # Directly call sandbox's file_write method, pass all parameters
        return await self.sandbox.file_write(
            file=file, 
            content=final_content,
            append=append,
            leading_newline=False,  # Already handled in final_content
            trailing_newline=False,  # Already handled in final_content
            sudo=sudo
        )
    
    @tool(parse_docstring=True)
    async def file_str_replace(
        self,
        file: _FileWriteTarget,
        old_str: str,
        new_str: str,
        sudo: Optional[bool] = False
    ) -> ToolResult:
        """Replace specified string in a file. Use for updating specific content in files or fixing errors in code.
        Patch all verified occurrences of the same defect together. Identical
        old/new text is a no-op, not a repair or a new revision.
        
        Args:
            file: Absolute path of the file to perform replacement on
            old_str: Original string to be replaced
            new_str: New string to replace with
            sudo: (Optional) Whether to use sudo privileges
        """
        # Directly call sandbox's file_replace method
        return await self.sandbox.file_replace(
            file=file,
            old_str=old_str,
            new_str=new_str,
            sudo=sudo
        )
    
    @tool_execution_contract(effects=("sandbox_read",))
    @tool(parse_docstring=True)
    async def file_find_in_content(
        self,
        file: str,
        regex: str,
        sudo: Literal[False] = False
    ) -> ToolResult:
        """Search for matching text within file content. Use for finding specific content or patterns in files.
        
        Args:
            file: Absolute path of the file to search within
            regex: Regular expression pattern to match
            sudo: Must remain false; core read-only tools do not support privilege elevation
        """
        # Directly call sandbox's file_search method
        return await self.sandbox.file_search(
            file=file,
            regex=regex,
            sudo=sudo
        )
    
    @tool_execution_contract(effects=("sandbox_read",))
    @tool(parse_docstring=True)
    async def file_find_by_name(
        self,
        path: str,
        glob: str
    ) -> ToolResult:
        """Find files by name pattern in specified directory. Use for locating files with specific naming patterns.
        
        Args:
            path: Absolute path of directory to search
            glob: Filename pattern using glob syntax wildcards
        """
        # Directly call sandbox's file_find method
        return await self.sandbox.file_find(
            path=path,
            glob_pattern=glob
        )
