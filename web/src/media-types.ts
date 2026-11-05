/** 管理端只持有公开元数据，内容通过认证接口单独取得。 */
export interface MediaAsset {
  id:string; purpose:'input'|'output'; mime_type:string; size_bytes:number; sha256:string;
  material_id?:string;
  status:'writing'|'ready'|'expired'|'failed'; created_at:string; expires_at:string;
}
export interface ModelMediaCapabilities {vision_enabled:boolean;image_api_profile:'none'|'gpt-image'|'legacy-b64';audio_chat_enabled:boolean}
export interface MediaGenerationRequest {
  model:string; session_id:string; prompt:string; output_type:'IMAGE'|'AUDIO';
  messages?:{role:'user'|'assistant';content:string}[];image_format?:string;audio_format?:string;voice?:string;
}
export interface MediaGenerationResult {execution_id:string;request_id:string;backend:'python-media';model:string;text:string|null;attachments:MediaAsset[];duration_ms:number;record_saved:boolean}
export interface MediaRun {execution_id:string;entry:'admin'|'qq';session_id:string;request_id:string;model:string;model_display_name:string;backend:'python-media';output_type:'IMAGE'|'AUDIO';status:'running'|'completed'|'failed'|'cancelled';duration_ms:number;error_code:string|null;created_at:string}
export interface MediaRunDetail extends MediaRun {prompt:string;content:string;attachments:MediaAsset[]}
