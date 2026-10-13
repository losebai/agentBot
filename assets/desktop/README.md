# 桌面图标

`agentbot-icon-v2.png` 是参考用户提供的粉白发狐耳人物图，用内置 imagegen 重绘后缩小耳朵的透明图标。
`agentbot-icon-v2.ico` 由 PyInstaller 的标准图标转换器编码，包含 16、24、32、48、64、128、256 像素尺寸。
`tools/windows.spec` 将 ICO 嵌入 GUI 和 CLI EXE；Windows 桌面窗口从 EXE 读取图标。
更新后重新执行 Windows 构建命令即可生效，既有 EXE 不会自动变化。

最终修改提示词（内置 imagegen，参考上一版生成图）：

> Edit the attached desktop application icon. Change ONLY the two fox ears: make both ears noticeably smaller, approximately 55–60% of their current height and 70% of their current width, with compact natural proportions relative to her head. Keep them fluffy, pointed, pink inside and white outside, attached naturally to the same head locations. Keep the original face, face size, eyes, expression, hairstyle, pink-white colors, sakura hair flower, red ribbon, kimono collar, rendering style and illustration quality unchanged. Keep the overall head and hair framing unchanged; do not scale down the whole portrait to simulate smaller ears. Maintain the square transparent canvas and genuine alpha background, with comfortable transparent margin above the smaller ears. No other design changes, no added objects, no text, no watermark.
