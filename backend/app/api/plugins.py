from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import (
    get_plugin_host_runtime,
    require_plugin_admin,
)
from app.plugins.bootstrap import (
    BuiltinPluginBuildDisabledError,
    PluginHostRuntimeLike,
)


PLUGIN_PACKAGE_MEDIA_TYPE = "application/vnd.matinier.plugin+zip"
router = APIRouter(prefix="/api/plugins", tags=["plugins"])


class PluginDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    name: str
    status: str
    preferred_version: str
    versions: list[str]
    permissions: list[str]
    runtime_status: str
    quarantine_reason: str | None


class BuiltinPluginSummaryResponse(BaseModel):
    """The intentionally small public projection of an internal descriptor."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    description: str
    version: str
    dynamic_build_available: bool


class InstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64)
    accepted_permissions: tuple[str, ...] = Field(max_length=128)
    trust_publisher: bool = False
    approved_publisher_fingerprint: str | None = Field(default=None, max_length=80)


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_session_id: str | None = Field(default=None, max_length=64)
    capability: str = Field(min_length=3, max_length=160)
    effect: Literal["read", "local_write", "network", "external_write"]
    scope: dict[str, object]
    ttl_seconds: int = Field(ge=30, le=86_400)


@router.post("/packages:inspect")
async def inspect_package(
    request: Request,
    _: None = Depends(require_plugin_admin),
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != PLUGIN_PACKAGE_MEDIA_TYPE:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Plugin package media type required",
        )
    maximum = request.app.state.settings.plugin_package_max_compressed_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = maximum + 1
        if declared > maximum:
            raise HTTPException(status_code=413, detail="Plugin package is too large")
    upload_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="upload-",
            suffix=".plugin.zip",
            dir=request.app.state.settings.plugin_staging_dir,
            delete=False,
        ) as stream:
            upload_path = Path(stream.name)
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > maximum:
                    raise HTTPException(
                        status_code=413,
                        detail="Plugin package is too large",
                    )
                stream.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="Plugin package is empty")
        return runtime.inspect_package(upload_path)
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Plugin package is invalid") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Plugin operation failed") from None
    finally:
        if upload_path is not None:
            upload_path.unlink(missing_ok=True)


@router.post(
    "/installations",
    response_model=PluginDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def install_plugin(
    payload: InstallRequest,
    _: None = Depends(require_plugin_admin),
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    try:
        return await runtime.confirm_install(
            ticket_id=payload.ticket_id,
            accepted_permissions=payload.accepted_permissions,
            trust_publisher=payload.trust_publisher,
            approved_publisher_fingerprint=payload.approved_publisher_fingerprint,
        )
    except (ValueError, LookupError):
        raise HTTPException(status_code=409, detail="Plugin installation rejected") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Plugin operation failed") from None


@router.get("/builtins", response_model=list[BuiltinPluginSummaryResponse])
def list_builtin_plugins(
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    try:
        return runtime.list_builtin_plugins()
    except Exception:
        raise HTTPException(status_code=500, detail="Plugin operation failed") from None


@router.post("/builtins/{plugin_id}/packages:inspect")
async def inspect_builtin_plugin(
    plugin_id: str,
    _: None = Depends(require_plugin_admin),
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    try:
        return await runtime.inspect_builtin_plugin(plugin_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Built-in plugin not found") from None
    except BuiltinPluginBuildDisabledError:
        raise HTTPException(
            status_code=409,
            detail="Built-in plugin build is disabled",
        ) from None
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Built-in plugin inspection failed",
        ) from None


@router.get("", response_model=list[PluginDetailResponse])
def list_plugins(
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    try:
        return runtime.list_plugins()
    except Exception:
        raise HTTPException(status_code=500, detail="Plugin operation failed") from None


@router.get("/{plugin_id}", response_model=PluginDetailResponse)
def get_plugin(
    plugin_id: str,
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    try:
        return runtime.detail(plugin_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Plugin not found") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Plugin operation failed") from None


@router.post("/{plugin_id}/enable", response_model=PluginDetailResponse)
async def enable_plugin(
    plugin_id: str,
    _: None = Depends(require_plugin_admin),
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    return await _lifecycle_call(runtime.enable_plugin, plugin_id)


@router.post("/{plugin_id}/disable", response_model=PluginDetailResponse)
async def disable_plugin(
    plugin_id: str,
    _: None = Depends(require_plugin_admin),
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    return await _lifecycle_call(runtime.disable_plugin, plugin_id)


@router.post("/{plugin_id}/grants")
def create_grant(
    plugin_id: str,
    payload: GrantRequest,
    _: None = Depends(require_plugin_admin),
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    try:
        return runtime.create_grant(plugin_id, **payload.model_dump())
    except LookupError:
        raise HTTPException(status_code=404, detail="Plugin not found") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="Plugin grant rejected") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Plugin operation failed") from None


@router.delete("/{plugin_id}", status_code=status.HTTP_204_NO_CONTENT)
async def uninstall_plugin(
    plugin_id: str,
    _: None = Depends(require_plugin_admin),
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
) -> Response:
    try:
        await runtime.uninstall_plugin(plugin_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Plugin not found") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Plugin operation failed") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _lifecycle_call(operation, plugin_id: str):
    try:
        return await operation(plugin_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Plugin not found") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="Plugin operation rejected") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Plugin operation failed") from None
