-- dispatch-chat GPT-SoVITS MCP 默认配置（MySQL）
-- 本文件幂等补齐字段且不会覆盖已有值；执行后默认仍为关闭状态。

SELECT GET_LOCK('dispatch_chat_setting', 5);
START TRANSACTION;

INSERT INTO `dispatch_chat_setting` (`config_key`, `config_value`, `description`) VALUES
    ('dispatch_chat_tts_enabled', 'false', '是否启用 GPT-SoVITS MCP 语音回复'),
    ('dispatch_chat_tts_url', '"http://127.0.0.1:9880/tts"', 'GPT-SoVITS /tts 完整接口地址'),
    ('dispatch_chat_tts_token', 'null', '已废弃，运行时不使用：旧 TTS 服务鉴权令牌'),
    ('dispatch_chat_tts_model', '"tts-1"', '旧原生音频模型兼容字段'),
    ('dispatch_chat_tts_voice', '"alloy"', '旧原生音频音色兼容字段'),
    ('dispatch_chat_tts_response_format', '"mp3"', '旧原生音频格式兼容字段'),
    ('dispatch_chat_tts_speed', '1.0', 'GPT-SoVITS 固定语速倍率'),
    ('dispatch_chat_tts_timeout_seconds', '30', 'GPT-SoVITS 请求超时秒数'),
    ('dispatch_chat_tts_max_input_chars', '1000', '单次 GPT-SoVITS 请求最大输入字符数'),
    ('dispatch_chat_tts_max_audio_bytes', '5242880', '单次 GPT-SoVITS 音频最大字节数'),
    ('dispatch_chat_tts_reference_audio_path', 'null', 'GPT-SoVITS 固定参考音频路径'),
    ('dispatch_chat_tts_reference_text', 'null', 'GPT-SoVITS 固定参考音频文本'),
    ('dispatch_chat_tts_reference_language', '"zh"', 'GPT-SoVITS 参考文本语言'),
    ('dispatch_chat_tts_text_language', '"zh"', 'GPT-SoVITS 目标文本语言'),
    ('dispatch_chat_tts_output_directory', '"data/tts"', 'GPT-SoVITS 生成音频目录')
ON DUPLICATE KEY UPDATE
    `description` = VALUES(`description`);

UPDATE `dispatch_chat_setting_revision`
SET `revision` = `revision` + 1
WHERE `id` = 1;

COMMIT;
SELECT RELEASE_LOCK('dispatch_chat_setting');
