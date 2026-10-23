# MiniMax H3 单图生成视频工作流

## UltraSharp 1080p 清晰增强版

`minimax_h3_r2v_ultrasharp1080p.json` 与 `minimax_h3_r2v_ultrasharp1080p_api.json` 使用 4x-UltraSharp 逐帧恢复细节，再经 RAFT-small 光流对齐及轻度三帧时序降噪，最终输出 1080×1920、8 秒、24 FPS，并保留音轨。先四倍模型增强，再用 Lanczos 缩至目标尺寸，不在已经超分过的视频上继续叠加。

需要将配套 `src/resources/h3_frame_upscale.py` 和 `src/resources/h3_temporal_consistency.py` 安装到 ComfyUI `custom_nodes/` 并重启实例。模型使用 `upscale_models/4x-UltraSharp.safetensors` 与 `optical_flow/raft_small_C_T_V2-01064c6d.pth`，节点执行不联网。逐帧节点复用 ComfyUI 原生分块与显存管理，成片帧采用半精度缓存。时序默认半尺寸光流、亮度强度 0.15、色彩强度 0.3，不改变帧率或音轨；两项强度设为 0 可跳过。部署步骤及模型来源见下方部署说明。该处理主要减少细节闪烁，不能保证修复原片的人体结构变化。

## 两倍版本

`minimax_h3_r2v_upscale2x.json` 与 `minimax_h3_r2v_upscale2x_api.json` 是 test 实例当前工作流的可移植版本。采用已安装的 ref2va 主模型、首帧 Guide、448×800 基础生成、192 帧、24 FPS，并接入 RealESRGAN_x2plus 两倍超分，输出 896×1600、8 秒带声音的 MP4。节点字段以实际运行的 ComfyUI v0.37.0 为准。

机器人现已支持 `image_to_video` 类型和独立 `local_workflow_generate_video` 工具。将配套 API JSON 导入工作流管理，启用后给目标 Agent 显式授权“工作流列表”和“工作流生视频”。H3 不支持负向提示词，也不能作为默认生图工作流。部署预算、媒体存储和同步目录说明见 [`minimax_h3_video_guide.md`](../../src/resources/minimax_h3_video_guide.md)。

以下是早期 fl2va 单图模板的设计记录，文件名为 `minimax_h3_i2v*`；其模型文件名与参数与当前 ref2va 模板不同。

这是机器人接入前的工作流设计产物：画布用于 ComfyUI 手工检查，API 模板用于后续服务端适配。已做离线节点与连线核对，尚未在实际 ComfyUI、模型和显卡上运行。

## 文件与节点链路

- `minimax_h3_i2v.json`：平铺 ComfyUI 画布，可在 ComfyUI 中打开，含中文节点标题与说明。
- `minimax_h3_i2v_api.json`：与画布一致的 API prompt 模板。整个对象放入 ComfyUI `/prompt` 请求的 `prompt` 字段，不直接把画布 JSON 提交到 `/prompt`。

```text
首帧图片 ───────────┐
文本／图片编码器 ──┼→ MiniMaxH3ImageToVideo ── 条件 → BasicGuider ──┐
视频 VAE ──────────┘                      └─ 潜变量 ─────────────┤
H3 主模型 ────────→ BasicGuider + BasicScheduler ─────────────────┤
RandomNoise + KSamplerSelect ────────────────────────────────────┤
                                                                ↓
                                                   SamplerCustomAdvanced
                                                        │ 同一输出
                                ┌───────────────────────┴──────────────────┐
                           视频 VAE 解码                              音频 VAE 解码
                                └──────────→ CreateVideo ←─────────────────┘
                                                  ↓
                                           SaveVideo：MP4
```

只有一个 `LoadImage`、一个采样器和一个保存输出。采用原生 H3 单条件引导，不绑定负向提示词；视频与声音来自同一次采样。

## 初始参数

| 参数 | 初值 | 修改位置与含义 |
| --- | --- | --- |
| 输入 | `h3_input.png` | 节点 1 的 `image`；这是待上传的文件名，不是附带的图片 |
| 描述 | 中文运动与环境声示例 | 节点 6 的 `prompt`；主体运动、镜头和声音写在同一段 |
| 尺寸 | 640×640 | 节点 6 的 `width` / `height`，均为 32 的倍数 |
| 帧数 | 124 | 节点 6 的 `length`；满足 `17k+5`，在 24 FPS 下约 5.17 秒 |
| 种子 | 1，固定 | 节点 8 的 `noise_seed`；重复手工运行便于对比 |
| 算法／调度 | `res_multistep` / `simple` | 节点 9 / 10 |
| 步数 | 20 | 节点 10；基础模型配置，没有加载 Turbo LoRA |
| 视频 | 24 FPS，8 bit | 节点 14 |
| 输出 | MP4 / H.264 | 节点 15；输出到 ComfyUI 的 `output/video/` |

约 5 秒不等于严格 5 秒：模型按 17 帧分组对齐，本模板直接固定 124 帧，不引入计算节点。后续修改帧数时保持 `length >= 5` 且 `length % 17 == 5`；音频长度随模型联合潜变量生成。

首帧会拉伸到所选画布。本版用正方形进行首次验证；横图或竖图应先修改画布比例，避免把画面拉伸误认为模型错误。本版不自动裁图、补边或改变用户上传图片。

## ComfyUI 与模型依赖

节点字段以 **ComfyUI v0.33.0** 为核对基线，提交为 `2f35f4a08176d993cded35dac3332be4f7287f41`。使用原生节点；模板没有调用云端付费接口，也未引入第三方节点包。量化模型是否能加载仍取决于实际 ComfyUI 依赖、硬件及权重。

| ComfyUI 子目录 | 模型文件 |
| --- | --- |
| `models/diffusion_models/` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` |
| `models/text_encoders/` | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` |
| `models/vae/` | `minimax_h3_video_vae_int8_convrot.safetensors` |
| `models/vae/` | `minimax_h3_audio_vae_fp32.safetensors` |

这些模型来自 [Comfy-Org 的 H3 模型仓库](https://huggingface.co/Comfy-Org/MiniMax-H3/tree/main)。本目录不包含权重，不会自动下载模型、安装节点或切换量化版本。

`SaveVideo` 的动态控件在不同版本中可能改变。本版 API 显式使用 `format=mp4`、`codec=h264`、`codec.encoding=auto`；后者是动态控件展开字段，不是普通顶层 `encoding`。升级 ComfyUI 后先核对实际 `/object_info/SaveVideo` 与导出的 API，不能把模板版本视为对所有更新版本的兼容承诺。

## 在 ComfyUI 中手工验证

1. 将四个模型放入对应目录，启动核对基线或已确认字段兼容的 ComfyUI。
2. 打开画布文件 `minimax_h3_i2v.json`。所有节点应能识别；在加载器中确认实际文件名。
3. 在“首帧图片”节点上传一张图片。示例文件名 `h3_input.png` 在上传前并不存在。
4. 修改节点 6 的运动描述；按图片比例调整尺寸，先保留 124 帧和 20 步进行验证。
5. 点击运行；确认节点 15 输出 MP4，画面从首帧生成并带有音轨。记录实际耗时及峰值显存，不推断未知显卡能够在特定时间内完成。
6. 若需要服务端调用，从同一 ComfyUI 再导出 API，比较节点 ID、模型文件名和 `SaveVideo` 动态字段。

离线检查不能证明实际模型可加载、生成质量、视频可解码或 QQ 可投递。真正运行 H3 才能验证这些项目。

## 后续机器人绑定契约

| 角色 | API 节点／字段 | 规则 |
| --- | --- | --- |
| 工作流类型 | `image_to_video` | 只允许由新增的视频工具选择 |
| 输入图片 | `1.inputs.image` | 服务端上传成功后写入 Comfy input 引用，只允许本请求唯一原图 |
| 正向提示词 | `6.inputs.prompt` | 覆盖示例文本；保留管理员附加词的合并规则 |
| 负向提示词 | 无 | 本模板不提供负向字段，不伪造空节点 |
| 种子 | `8.inputs.noise_seed` | 每次请求生成，不能向节点 11 增加无效的 `seed` |
| 输出 | 节点 `15` | 只读取该节点的视频结果，不能取节点 12 的图片预览 |
| 输出格式 | `video/mp4` | 本地流式下载并按视频预算、容器结构验证 |
| 固定参数 | 节点 2—5、6 的尺寸与帧数、9—10、14—15 | 由管理员维护，不作为 Agent 工具参数开放 |

当前机器人导入器及执行链路尚不支持上述视频契约，不能将此 API 当作已可执行的生图工作流启用。本次只交付工作流；后续按已确认设计接入视频工具、队列、存储及投递。

## 来源

- [官方 H3 图生视频模板](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_i2v.json)：采用其原生音视频链路，展开子图并去掉未启用的 Turbo 分支、末帧和辅助参数节点。
- [H3 节点定义（固定版本）](https://github.com/Comfy-Org/ComfyUI/blob/2f35f4a08176d993cded35dac3332be4f7287f41/comfy_extras/nodes_minimax_h3.py)：首帧、描述、尺寸、帧数及联合潜变量接口。
- [视频保存节点（固定版本）](https://github.com/Comfy-Org/ComfyUI/blob/2f35f4a08176d993cded35dac3332be4f7287f41/comfy_extras/nodes_video.py)：MP4、H.264 动态控件和音视频合成接口。
