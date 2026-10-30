// 项目内 Node 入口：路径来自所属后端，网页不能提供命令或目录。
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const workdir = process.env.NAPCAT_WORKDIR;
if (!workdir || !path.isAbsolute(workdir)) {
  throw new Error('NapCat instance work directory is required');
}
const wrapperPath = path.join(__dirname, 'wrapper.node');
const qqDataPath = path.join(workdir, 'qq');
fs.mkdirSync(qqDataPath, { recursive: true });
process.env.NAPCAT_WRAPPER_PATH = wrapperPath;
process.env.NAPCAT_QQ_PACKAGE_INFO_PATH = path.join(__dirname, 'package.json');
process.env.NAPCAT_QQ_VERSION_CONFIG_PATH = path.join(__dirname, 'config.json');

// Windows 原生资料发现不遵循 USERPROFILE；在 SDK 会话初始化前固定资料路径。
// 只包装本载荷的 wrapper.node，其余原生模块仍使用原生加载与方法接收者。
const originalDlopen = process.dlopen;
process.dlopen = function(module, filename, ...args) {
  const result = originalDlopen.call(this, module, filename, ...args);
  if (path.resolve(filename).toLowerCase() === wrapperPath.toLowerCase()) {
    const util = module.exports.NodeQQNTWrapperUtil;
    if (!util || typeof util.getNTUserDataInfoConfig !== 'function') {
      throw new Error('NapCat native data path API is unavailable');
    }
    // 使用独立代理对象，不改写原生对象中可能不可写的属性。
    const isolatedUtil = new Proxy(Object.create(null), {
      get(_target, key) {
        if (key === 'getNTUserDataInfoConfig') return () => qqDataPath;
        const value = Reflect.get(util, key, util);
        return typeof value === 'function' ? value.bind(util) : value;
      },
    });
    const descriptors = Object.getOwnPropertyDescriptors(module.exports);
    descriptors.NodeQQNTWrapperUtil = { value: isolatedUtil, enumerable: true };
    module.exports = Object.create(Object.getPrototypeOf(module.exports), descriptors);
  }
  return result;
};

void import(pathToFileURL(path.join(__dirname, 'napcat', 'napcat.mjs')).href);
