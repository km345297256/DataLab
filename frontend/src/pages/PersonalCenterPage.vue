<template>
  <main class="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto bg-[var(--background-gray-main)] text-[var(--text-primary)]">
    <header class="mobile-safe-top flex min-h-[64px] shrink-0 items-center gap-3 border-b border-[var(--border-main)] px-4 py-3 sm:px-6">
      <button v-if="!isLeftPanelShow" type="button" class="center-icon-button" aria-label="打开导航" @click="showLeftPanel">
        <PanelLeft class="size-5" />
      </button>
      <span class="text-sm font-medium">个人中心</span>
      <RouterLink to="/chat" class="ml-auto inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-sm text-[#2b7659] hover:bg-[var(--fill-tsp-gray-main)] dark:text-[#a9cbbb]">
        <ArrowLeft class="size-4" />返回分析
      </RouterLink>
    </header>

    <div class="mx-auto w-full max-w-[920px] px-5 py-8 sm:px-10 sm:py-12">
      <section aria-labelledby="account-title" class="flex items-center gap-4">
        <span class="flex size-12 shrink-0 items-center justify-center rounded-full bg-[#2b7659]/10 text-xl font-semibold text-[#2b7659] dark:text-[#a9cbbb]" aria-hidden="true">{{ avatarLetter }}</span>
        <div class="min-w-0">
          <h1 id="account-title" class="truncate text-xl font-semibold">{{ currentUser?.fullname || '个人中心' }}</h1>
          <p class="mt-1 text-xs text-[var(--text-tertiary)]">{{ currentUser?.role === 'admin' ? '管理员' : currentUser?.role === 'software' ? '服务账号' : '普通用户' }} · 独立数据与分析空间</p>
          <p v-if="currentUser?.email" class="mt-1 truncate text-sm text-[var(--text-tertiary)]">{{ currentUser.email }}</p>
        </div>
      </section>

      <section class="mt-10" aria-labelledby="workspace-management-title">
        <h2 id="workspace-management-title" class="mb-4 text-sm font-semibold">我的工作空间</h2>
        <div class="grid gap-3 sm:grid-cols-2">
          <RouterLink v-for="entry in managementEntries" :key="entry.to" :to="entry.to" class="center-entry">
            <span class="center-entry-icon"><component :is="entry.icon" class="size-5" /></span>
            <span class="min-w-0 flex-1">
              <span class="block text-sm font-medium">{{ entry.title }}</span>
              <span class="mt-1 block text-xs leading-5 text-[var(--text-tertiary)]">{{ entry.description }}</span>
            </span>
            <ChevronRight class="size-4 shrink-0 text-[var(--icon-tertiary)]" />
          </RouterLink>
          <button type="button" class="center-entry" @click="openAccountSettings">
            <span class="center-entry-icon"><Settings2 class="size-5" /></span>
            <span class="min-w-0 flex-1">
              <span class="block text-sm font-medium">账号与设置</span>
              <span class="mt-1 block text-xs leading-5 text-[var(--text-tertiary)]">修改姓名与密码，管理当前登录账号</span>
            </span>
            <ChevronRight class="size-4 shrink-0 text-[var(--icon-tertiary)]" />
          </button>
        </div>
      </section>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { RouterLink } from 'vue-router';
import { ArrowLeft, ChartColumn, ChevronRight, FolderOpen, PanelLeft, PlugZap, Puzzle, Settings2, ShieldCheck } from 'lucide-vue-next';
import { useAuth } from '../composables/useAuth';
import { useLeftPanel } from '../composables/useLeftPanel';
import { useSettingsDialog } from '../composables/useSettingsDialog';

const { currentUser, isAdmin } = useAuth();
const { isLeftPanelShow, showLeftPanel } = useLeftPanel();
const { openSettingsDialog } = useSettingsDialog();
const avatarLetter = computed(() => currentUser.value?.fullname?.charAt(0)?.toUpperCase() || 'D');
const managementEntries = computed(() => [
  { to: '/my-data', title: '我的数据', description: '管理保存的数据、文件与目录', icon: FolderOpen },
  { to: '/skills', title: '分析技能库', description: '查看和管理分析使用的技能', icon: Puzzle },
  { to: '/connectors', title: '数据连接器', description: '查看可用的开放数据来源', icon: PlugZap },
  { to: '/visualizations', title: '可视化设置', description: '选择文件预览视图，查看支持格式与范围', icon: ChartColumn },
  ...(isAdmin.value ? [{ to: '/chat/admin', title: '系统管理', description: '管理用户、任务与系统配置', icon: ShieldCheck }] : []),
]);

function openAccountSettings() {
  openSettingsDialog('account');
}
</script>

<style scoped>
.center-icon-button { display: flex; height: 36px; width: 36px; align-items: center; justify-content: center; border-radius: 8px; }
.center-icon-button:hover { background: var(--fill-tsp-gray-main); }
.center-entry { display: flex; min-width: 0; align-items: center; gap: 12px; padding: 20px 16px; border: 1px solid var(--border-main); border-radius: 10px; background: var(--background-menu-white); text-align: left; transition: border-color 150ms, background-color 150ms; }
.center-entry:hover { border-color: #9bbdad; background: var(--fill-tsp-white-light); }
.center-entry:focus-visible { outline: 2px solid #2b7659; outline-offset: 3px; }
.center-entry-icon { display: flex; height: 36px; width: 36px; flex-shrink: 0; align-items: center; justify-content: center; border-radius: 8px; color: #2b7659; background: #2b76590d; }
:global(.dark) .center-entry-icon { color: #a9cbbb; background: #a9cbbb12; }
</style>
