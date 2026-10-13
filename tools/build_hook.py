"""为平铺源码布局提供标准 editable 包映射，普通 wheel 沿用发布路径配置。"""

from pathlib import Path
from typing import Any

from editables import EditableProject
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """仅在开发安装时将 agent_bot 映射到 src，避免移动源码或复制运行模块。"""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """生成标准导入映射并随 editable wheel 发布所需依赖。"""
        if version != "editable":
            return
        project = EditableProject(self.metadata.core.name, self.root)
        project.map("agent_bot", "src\\agent_bot")
        self._editable_paths: list[Path] = []
        for filename, content in project.files():
            if filename.endswith(".py"):
                # SQL 强制打包会形成同名命名空间目录，源码映射必须先于该目录解析。
                content += "\nimport sys\nsys.meta_path.remove(F)\nsys.meta_path.insert(0, F)\n"
            path = Path(self.directory) / filename
            path.write_text(content, encoding="utf-8")
            build_data["force_include"][str(path)] = filename
            self._editable_paths.append(path)
        build_data["dependencies"].extend(project.dependencies())

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        """打包完成后移除本钩子生成的临时文件，保留 wheel 和源码。"""
        if version == "editable":
            for path in self._editable_paths:
                path.unlink()
