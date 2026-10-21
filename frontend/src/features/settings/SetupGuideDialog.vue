<script setup lang="ts">
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
defineProps<{ firstLaunch?: boolean }>();
const emit = defineEmits<{ close: []; navigate: [path: string] }>();
</script>
<template>
  <Dialog :open="true" :title="firstLaunch ? '欢迎使用 · 初次配置' : '使用指南'" @update:open="emit('close')">
    <div class="setup-guide">
      <p class="intro">按以下五步配置价格和供应商。按钮会打开对应页面并定位到操作位置；之后可在“设置 → 指南与关于”中重新查阅。</p>
      <ol>
        <li><h3>在模型池配置基准价格</h3>
          <p>1. 点击“查找最近使用的模型”，选中模型后加入模型池，也可以手动添加。</p>
          <p>2. 为模型填写输入、输出、缓存命中和缓存创建价格，单位为 USD / 百万 Token，再点击“保存全部修改”。缓存创建价留空按 0，其余价格留空表示未定价。</p>
          <p>模型池保存基准价，之后可用倍率填充各供应商的实际价格。</p>
          <Button @click="emit('navigate', '/models/setup')">配置模型池价格</Button>
        </li>
        <li><h3>创建价格组</h3>
          <p>在“价格与模型 → 价格组”中新建价格组。建议用供应商名称命名，例如“供应商 A”，方便区分不同站点的价格；同一供应商有多套方案时也可分别建组。</p>
          <Button @click="emit('navigate', '/pricing/groups')">定位新建价格组</Button>
        </li>
        <li><h3>配置倍率版本</h3>
          <p>1. 选中价格组，进入“倍率版本管理”，使用默认版本或新增版本。</p>
          <p>2. 建议按计费倍率命名，例如“0.8 倍”或“1.2 倍”。版本名称用于辨认方案，实际费用仍由下一步保存的模型价格决定。</p>
          <Button @click="emit('navigate', '/pricing/versions')">打开倍率版本管理</Button>
        </li>
        <li><h3>编辑实际计费价格</h3>
          <p>1. 在“价格编辑”中选择版本，点击“添加模型行”，从模型池选择模型。</p>
          <p>2. 直接填写实际价格，或为单个模型输入实际计费倍率，再点击“按倍率填充”。检查各项价格后，点击“保存（追加修订）”。</p>
          <p>3. 回到所用 Harness 页面，选择对应的价格组与版本。新价格用于之后的请求；需要更新已有费用时，通过“重算历史…”先预览再确认。</p>
          <Button @click="emit('navigate', '/pricing/rates')">打开价格编辑</Button>
        </li>
        <li><h3>配置供应商与钱包</h3>
          <p>1. 添加供应商并填写名称、网址。过往充值和过往消费可选填，用于补充使用本软件前的记账总额；留空按 0，不改变当前余额。</p>
          <p>2. 关联该供应商的价格组，让后续本地估算消费记入相应钱包。同一供应商可以关联多个价格组。</p>
          <p>3. 需要供应商提供的余额与账单依据时，切换到“远程钱包 → 远程账户”，编辑账户并选择实际接口类型。</p>
          <ul>
            <li><strong>New API：</strong>到供应商网站的“个人资料 → 安全 → 访问令牌”生成并复制访问令牌，同时填写用户 ID。模型调用用的 API 密钥不能替代该令牌。</li>
            <li><strong>sub2api：</strong>使用该供应商账户下的任意 API 密钥，可在供应商网站的 API 密钥页面创建或复制。</li>
            <li><strong>DeepSeek 官方：</strong>填写官方 API 密钥。</li>
          </ul>
          <p>4. 测试连接后保存。远程账单的准确程度取决于站点提供的字段；只有余额的接口仍需结合本地估算。未配置远程账户也可使用本地钱包记账。</p>
          <Button @click="emit('navigate', '/wallet/setup/new')">定位添加供应商</Button>
        </li>
      </ol>
      <section v-if="!firstLaunch" class="later-guide">
        <h3>日常使用：处理未定价模型</h3>
        <p>用量页出现未定价提醒时，打开“价格与模型 → 未定价模型相关”，选择价格组、版本和计价模型，预览后补价。也可忽略选中的既有请求，或恢复之前忽略的项目。</p>
        <p>关闭提醒只隐藏提示，未定价请求仍可在此页面处理。补价更新本地估算；供应商已确认的远程消费单独保存。</p>
        <Button @click="emit('navigate', '/unpriced')">打开未定价模型</Button>
      </section>
      <p class="text-muted">本地费用是按已保存价格计算的估算值。请先在“设置 → 采集来源”检查 Harness 路径；需要导入旧数据时，前往“设置 → 数据与历史”。</p>
    </div>
    <template #footer><Button @click="emit('close')">{{ firstLaunch ? '稍后设置' : '关闭' }}</Button><Button v-if="firstLaunch" variant="primary" @click="emit('navigate', '/models/setup')">开始设置</Button></template>
  </Dialog>
</template>
<style scoped>
.setup-guide { display:grid;gap:20px;font-size:13px;line-height:1.8; }
.intro { color:var(--text-muted); }ol { margin:0;padding-left:24px;display:grid;gap:24px; }
li { padding-left:6px; }li::marker { color:var(--accent);font-weight:700; }h3 { font-size:15px;margin-bottom:8px; }
li p + p, .later-guide p + p { margin-top:8px; }li .button,.later-guide .button { margin-top:12px; }
ul { padding-left:18px;margin-top:8px;display:grid;gap:8px; }.later-guide { border-top:1px solid var(--border);padding-top:18px; }
</style>
