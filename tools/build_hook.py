"""为源码及外置资源提供标准 editable 包映射，普通 wheel 沿用发布路径配置。"""

from pathlib import Path
from typing import Any

from editables import EditableProject
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """开发入口保留已安装资源路径，业务子模块仍直接从源码目录导入。"""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """生成标准导入映射并随 editable wheel 发布所需依赖。"""
        # Git 忽略不能导致发布缺少升级资源；缺失时在打包前明确失败。
        for filename in ('bilibili-init.sql', 'bilibili-init.sqlite.sql', 'UPGRADE-BILIBILI.md',
                         'startup-versioning.sql', 'startup-versioning.sqlite.sql',
                         'UPGRADE-AUTOMATIC.md', '2026-10-28-comfy-plugin.sql',
                         '2026-10-28-comfy-plugin.sqlite.sql'):
            if not (Path(self.root) / 'sql' / filename).is_file():
                raise FileNotFoundError(f'required database resource missing: {filename}')
        if version != "editable":
            return
        project = EditableProject(self.metadata.core.name, self.root)
        # 标准包入口位于安装目录，资源读取与正式 wheel 一致；__path__ 只重定向业务源码。
        project.add_to_subpackage("agent_bot", "src/agent_bot")
        source_init = str(Path(self.root) / "src/agent_bot/__init__.py")
        self._editable_paths: list[Path] = []
        for filename, content in project.files():
            # 执行原入口保留实时版本等包属性，不复制业务模块或增加仓库资源回退路径。
            content += (
                "\nfrom pathlib import Path as _Path\n"
                f"_source_init = _Path({source_init!r})\n"
                "exec(compile(_source_init.read_text(encoding='utf-8'), "
                "str(_source_init), 'exec'))\n"
                "del _Path, _source_init\n"
            )
            path = Path(self.directory) / filename.replace("/", "_")
            path.write_text(content, encoding="utf-8")
            build_data["force_include"][str(path)] = filename
            self._editable_paths.append(path)
        build_data["dependencies"].extend(project.dependencies())

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        """打包完成后移除本钩子生成的临时文件，保留 wheel 和源码。"""
        if version == "editable":
            for path in self._editable_paths:
                path.unlink()
