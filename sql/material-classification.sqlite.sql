-- 用途：为持久素材新增多对多来源类型和规范化标签，供管理与会话工具精确筛选。
-- 顺序：适用于已具备 chat_material 的 0.5.5 至 0.5.9 部署，由 material-classification-v1 登记后执行。
-- 数据影响：仅新增关联表和索引，不改写旧素材，不猜测或回填历史分类和标签。
-- 持久素材与实际来源类型关联。
CREATE TABLE IF NOT EXISTS chat_material_category (
    -- 所属持久素材 ID，外键保证来源类型归属于真实素材。
    material_id TEXT NOT NULL REFERENCES chat_material(id),
    -- 来源类型 received、sent、generated 或 uploaded。
    category TEXT NOT NULL,
    -- 同一素材保留多个来源，复用原件不会覆盖先前分类。
    PRIMARY KEY (material_id,category)
);
-- 类型在前支持精确类型筛选，素材在后供 EXISTS 关联和覆盖读取。
CREATE INDEX IF NOT EXISTS idx_material_category_lookup
ON chat_material_category(category,material_id);

-- 持久素材与可编辑主题标签关联。
CREATE TABLE IF NOT EXISTS chat_material_tag (
    -- 所属持久素材 ID，外键保证标签不能引用不存在的素材。
    material_id TEXT NOT NULL REFERENCES chat_material(id),
    -- 经 NFKC、去首尾空白和大小写折叠的主题标签。
    tag TEXT NOT NULL,
    -- 同一素材标签唯一，应用层限制完整标签组最多八个。
    PRIMARY KEY (material_id,tag)
);
-- 标签在前支持精确筛选，避免多标签 JOIN 导致分页和计数重复。
CREATE INDEX IF NOT EXISTS idx_material_tag_lookup ON chat_material_tag(tag,material_id);
