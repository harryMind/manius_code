from pathlib import Path


# 解析用户路径并拒绝任何逃离当前工作区的文件访问。
def resolve_workspace_path(value: str, workspace: Path | None = None) -> Path:
    workspace = (workspace or Path.cwd()).expanduser().resolve()
    candidate = Path(value).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    try:
        path.relative_to(workspace)
    except ValueError as error:
        raise ValueError(f"path must stay within the workspace: {value}") from error
    return path
