/** 工作流目录公开元数据；服务端路径和节点映射仅用于协议，不作为普通表单控件。 */
export interface WorkflowSummary {
  workflow_key: string;
  display_name: string;
  source_file: string;
  source_format: 'ui' | 'api';
  content_sha256: string;
  revision: string;
  enabled: boolean;
  executable: boolean;
  description: string;
  additional_positive_prompt: string;
  additional_negative_prompt: string;
  workflow_type: string;
  positive_node_id: string | null;
  negative_node_id: string | null;
  output_node_id: string | null;
  seed_node_id: string | null;
  input_image_node_id: string | null;
  positive_input_name: string;
  negative_input_name: string;
}

/** JSON 只在用户打开预览后读取，列表不携带完整文件内容。 */
export interface WorkflowDetail extends WorkflowSummary {
  ui_json: string | null;
  api_json: string | null;
}

/** 工作流版本与运行配置版本各自控制不同写入边界。 */
export interface WorkflowCatalog {
  items: WorkflowSummary[];
  default_key: string;
  settings_revision: number;
  directory_configured: boolean;
  next_cursor: string | null;
}

/** 网页允许编辑名称、说明、附加提示词和启用状态，不允许修改执行节点映射。 */
export type WorkflowMetadata = Pick<WorkflowSummary,
  'display_name' | 'description' | 'additional_positive_prompt' | 'additional_negative_prompt'>;
export type WorkflowPatch = Partial<WorkflowMetadata & Pick<WorkflowSummary, 'enabled'>>;
