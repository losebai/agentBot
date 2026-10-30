"""验证 Node 入口在加载原生组件后、启动会话前固定实例资料路径。"""

import os
import subprocess
from pathlib import Path


def test_node_entry_overrides_native_profile_without_calling_host_path(tmp_path):
    """即便原生方法会返回日常 QQ 目录，也只向 NapCat 提供实例目录。"""
    from agent_bot.napcat import payload

    node = tmp_path / "node"
    napcat = node / "napcat"
    napcat.mkdir(parents=True)
    entry = node / "agentbot-entry.cjs"
    entry.write_bytes(Path(payload.__file__).with_name("node_entry.cjs").read_bytes())
    preload = tmp_path / "sdk-stub.cjs"
    preload.write_text("""
// 不加载任何原生 DLL，也不读取系统 QQ 资料。
process.dlopen = (module, filename) => {
  const util = { marker: 'native' };
  Object.defineProperty(util, 'getNTUserDataInfoConfig', {
    value() { throw new Error('host QQ path must not be queried'); },
  });
  util.otherMethod = function() { return this.marker; };
  module.exports = { NodeQQNTWrapperUtil: util, untouched: 42 };
};
""", encoding="utf-8")
    (napcat / "napcat.mjs").write_text("""
import assert from 'node:assert/strict';
import path from 'node:path';
const addon = {};
process.dlopen(addon, process.env.NAPCAT_WRAPPER_PATH);
assert.equal(addon.exports.NodeQQNTWrapperUtil.getNTUserDataInfoConfig(),
  path.join(process.env.NAPCAT_WORKDIR, 'qq'));
assert.equal(addon.exports.NodeQQNTWrapperUtil.otherMethod(), 'native');
assert.equal(addon.exports.untouched, 42);
assert.deepEqual(Object.keys(addon.exports), ['NodeQQNTWrapperUtil', 'untouched']);
assert.equal(process.env.NAPCAT_QQ_PACKAGE_INFO_PATH,
  path.join(process.argv[1], '..', 'package.json'));
console.log('isolated native data path');
""", encoding="utf-8")
    work = tmp_path / "owned work"
    work.mkdir()
    result = subprocess.run(
        ["node", "--require", str(preload), str(entry)],
        env={**os.environ, "NAPCAT_WORKDIR": str(work)},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated native data path"
