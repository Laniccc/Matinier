from fastapi import HTTPException

def reject_legacy_meeting_write():
    raise HTTPException(410, {"code": "meeting_plugin_migration_required", "message": "请使用统一助手工作区的主程序操作区；旧会议写入接口已关闭。"})
