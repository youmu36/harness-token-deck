<script setup lang="ts">
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
defineProps<{ firstLaunch?: boolean }>();
const emit = defineEmits<{ close: []; navigate: [path: string] }>();
</script>
<template>
  <Dialog :open="true" :title="firstLaunch ? '欢迎使用 · 简要使用指南' : '简要使用指南'" @update:open="emit('close')">
    <div class="setup-guide">
      <p class="intro">按下面的顺序完成配置。之后可随时在“设置”中再次查阅。</p>
      <ol>
        <li><h3>扫描并添加模型池</h3><p>打开“价格与模型 → 模型池”，点击“查找最近使用的模型”，选中识别到的模型并点击“加入选中模型”。也可以手动添加。填写普通输入、缓存读取和输出的基准价格，单位为 USD / 百万 token；编辑后点击“保存全部修改”。</p><Button @click="emit('navigate', '/models')">打开模型池</Button></li>
        <li><h3>准备倍率版本</h3><p>进入“价格组”，没有价格组时先新建一个。页面默认打开“倍率版本管理”，先确认要使用的版本；默认版本即可开始，需要另一套价格时再新增版本。选好版本后进入价格编辑。</p><Button @click="emit('navigate', '/pricing/versions')">打开倍率版本管理</Button></li>
        <li><h3>编辑价格组并添加模型</h3><p>在“价格编辑”中点击“添加模型行”，从模型池选择模型。可以直接填写价格，也可以输入倍率后点击“按倍率填充”，最后点击“保存（追加修订）”。留空的价格表示未定价；实际计费使用已保存的价格。</p><p class="text-muted">为使用中的 Harness 选择价格组与版本，配置才会用于计费。新价格从生效时间起应用；最近 72 小时的已有账单，可通过“重算历史…”先预览再更新。</p><Button @click="emit('navigate', '/pricing')">打开价格组</Button></li>
        <li><h3>添加供应商与钱包</h3><p>打开“供应商与钱包”，扫描供应商并填写名称，或手动新建。在价格组中关联对应供应商，使后续消费记入该钱包。同一供应商可以关联多个价格组。</p><p>钱包中可以充值或校准余额。需要查询远程余额时，进入“远程账户 → 编辑远程账户”，显式选择接口：New API 填写用户 ID 和访问令牌，sub2api 填写 API 令牌，DeepSeek 官方填写 API 密钥。测试连接后保存，有效余额按获取时点显示；累计消费优先用供应商累计增量核算，无此能力时根据余额与本地成本推算。</p><Button @click="emit('navigate', '/wallet')">打开供应商与钱包</Button></li>
        <li><h3>处理未定价模型</h3><p>统计页顶部会提示最近 30 天未定价用量，可跳转补价。为原价格组补价后刷新本地估算；本地估算与账户消费分别保存。忽略只处理所选既有请求，不改变原始缺价事实；关闭提醒只隐藏当前提醒，新问题仍会提示，也可恢复提醒。标题辅助调用按 0.001 USD 估算，不属于未定价模型。</p><Button @click="emit('navigate', '/unpriced')">打开未定价模型</Button></li>
      </ol>
      <p><strong>估算价格：</strong>统计费用根据所选价格组与倍率版本中已保存的价格计算，不代表准确的真实消耗。配置远程账户后，账户消费按供应商累计字段核算或余额推算，并显示依据。充值是区间净流入（可能含赠送、退款），不代表真实次数或现金支付；可在供应商设置中人工改判，余额不变。本地日消费在次日结算，23:59 是当日标签，跨日账户区间保留独立起止时间。</p>
      <p class="text-muted">需要导入旧版数据库，可前往“设置 → 数据与历史”。凭据仅用于远程账户，模型池与价格组无需填写密钥。</p>
    </div>
    <template #footer><Button @click="emit('close')">{{ firstLaunch ? '稍后设置' : '关闭' }}</Button><Button v-if="firstLaunch" variant="primary" @click="emit('navigate', '/models')">开始设置</Button></template>
  </Dialog>
</template>
<style scoped>
.setup-guide { display: grid; gap: 18px; font-size: 13px; line-height: 1.7; }
.intro { color: var(--text-muted); }ol { margin: 0; padding-left: 24px; display: grid; gap: 22px; }
li { padding-left: 6px; }li::marker { color: var(--accent); font-weight: 700; }h3 { font-size: 15px; margin-bottom: 8px; }
li p + p { margin-top: 8px; }li .button { margin-top: 10px; justify-self: start; }
</style>
