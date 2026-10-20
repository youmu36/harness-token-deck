<template>
  <div class="management-layout">
    <aside class="management-rail"><slot name="rail" /></aside>
    <div class="management-workspace"><slot /></div>
  </div>
</template>
<style scoped>
.management-layout { display: grid; grid-template-columns: 224px minmax(0, 1fr); gap: 24px; align-items: start; min-height: 0; }
.management-rail { position: sticky; top: 16px; min-width: 0; max-height: calc(100dvh - 48px); overflow-y: auto; overscroll-behavior: contain; }
.management-workspace { min-width: 0; display: grid; gap: 20px; }
@media (max-width: 700px) { .management-layout { grid-template-columns: minmax(0, 1fr); gap: 16px; }.management-rail { position: static; } }
</style>
