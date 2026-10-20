import type { CollectionSnapshot, UsageHarness } from './types';

const states = {
  watching: { label: '监测中', tone: 'success' }, baselining: { label: '正在建立检查点', tone: 'neutral' },
  degraded: { label: '采集需关注', tone: 'warning' }, unavailable: { label: '来源不可用', tone: 'danger' },
  pending: { label: '等待连接', tone: 'neutral' },
} as const;
export function collectionIndicator(status: CollectionSnapshot | undefined, harness: UsageHarness) {
  if (status?.paused || status?.state === 'paused') return { label: '采集已暂停', tone: 'neutral' } as const;
  if (!status) return states.pending;
  const sources = status.sources.filter(source => harness === 'all' || source.harness === harness);
  // Overview surfaces the source that needs the most attention.
  for (const state of ['unavailable', 'degraded', 'baselining', 'pending', 'watching'] as const) {
    if (sources.some(source => source.status === state)) return states[state];
  }
  return states.pending;
}

const diagnostics: Record<string, string> = {
  desktop_source_unverified: '已排除无法验证为 Codex 桌面端的来源',
  request_usage_format_unverified: '部分桌面会话仅有累计快照，缺少可计费的请求级用量；未估算补入',
  source_directory_unavailable: '无法访问来源目录', source_database_unavailable: '无法读取来源数据库',
  source_schema_unsupported: '来源数据库格式不受支持', source_database_busy: '来源数据库忙，等待重试',
  source_read_failed: '无法读取来源文件，等待重试', source_stat_failed: '无法读取来源文件属性',
  source_changed_during_read: '读取期间来源已变化，等待重试', source_rewritten: '检测到来源改写，已重新检查并去重',
  source_decode_failed: '无法解压来源转录，已跳过该文件', source_file_too_large: '来源文件超过读取上限，已跳过该文件',
  source_truncated: '来源转录超过单次读取上限，已统计到截断处', source_line_too_large: '来源单行超过读取上限，采集暂停于该位置',
  invalid_json: '部分记录格式损坏，已跳过并保留诊断', invalid_usage_json: '用量字段格式损坏',
  invalid_token_count: '来源 token 数无效', invalid_attempt_index: '来源重试序号无效',
  model_name_invalid: '来源模型名去除空白后为空', cache_exceeds_input: '来源缓存量超过输入量',
  reasoning_exceeds_output: '来源推理量超过输出量', missing_timestamp: '来源缺少完成时间', invalid_timestamp: '来源完成时间无效',
  missing_session_id: '来源缺少会话标识', missing_request_id: '来源缺少请求标识', missing_response_id: '来源缺少请求响应标识',
  missing_usage: '来源缺少请求用量', missing_turn: '来源缺少 turn 标识', missing_step: '来源缺少 step 标识',
  session_identity_mismatch: '来源会话标识不一致', pending_request_removed: '尚未完成的请求已从来源移除',
  source_status_unsupported: '来源请求状态不受支持', invalid_checkpoint: '采集检查点损坏，已从头重新检查',
  source_path_changed: '来源路径已变更，等待重新连接', watcher_unavailable_polling: '文件通知不可用，使用定期检查',
};
export function diagnosticText(code: string): string {
  return diagnostics[code] ?? code;
}
