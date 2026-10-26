<template>
  <nav
    aria-label="主导航"
    class="flex h-full shrink-0 flex-col border-r border-[var(--border-main)] bg-[var(--background-menu-white)] transition-[width] duration-200 max-sm:fixed max-sm:inset-y-0 max-sm:left-0 max-sm:z-50"
    :class="isLeftPanelShow ? 'w-[276px] max-sm:w-[min(88vw,320px)]' : 'w-[52px] max-sm:w-0 max-sm:pointer-events-none max-sm:overflow-hidden'">
    <header class="mobile-safe-top flex min-h-[64px] shrink-0 items-center gap-2 border-b border-[var(--border-main)] px-2" :class="isLeftPanelShow ? 'px-4' : 'justify-center'">
      <button v-if="isLeftPanelShow" type="button" class="min-w-0 rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#2b7659]" aria-label="DataLab，新建分析" title="新建分析" @click="startNewAnalysis">
        <ManusLogoTextIcon :width="148" :height="34" />
      </button>
      <button type="button" class="navigation-icon-button" :class="isLeftPanelShow ? 'ml-auto' : ''" :aria-label="isLeftPanelShow ? '收起导航' : '打开导航'" :title="isLeftPanelShow ? '收起导航' : '打开导航'" @click="toggleLeftPanel">
        <PanelLeft class="size-5 text-[var(--icon-secondary)]" :class="isLeftPanelShow ? '' : 'rotate-180'" />
      </button>
    </header>

    <div class="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-2 py-4">
      <RouterLink to="/chat" class="navigation-item mb-3" :class="isLeftPanelShow ? '' : 'justify-center'" aria-label="返回分析" title="返回分析">
        <ChartNoAxesCombined class="size-[18px] shrink-0" />
        <span v-if="isLeftPanelShow" class="truncate text-sm">返回分析</span>
      </RouterLink>
      <p v-if="isLeftPanelShow" class="mb-1 px-3 text-xs font-medium text-[var(--text-tertiary)]">个人中心</p>
      <RouterLink to="/account" class="navigation-item" :class="[isLeftPanelShow ? '' : 'justify-center', route.name === 'PersonalCenter' ? 'navigation-item-active' : '']" :aria-current="route.name === 'PersonalCenter' ? 'page' : undefined" aria-label="个人中心" title="个人中心">
        <UserRound class="size-[18px] shrink-0" />
        <span v-if="isLeftPanelShow" class="truncate text-sm">个人中心</span>
      </RouterLink>
      <button type="button" class="navigation-item" :class="[isLeftPanelShow ? '' : 'justify-center', route.name === 'MyData' ? 'navigation-item-active' : '']" :aria-current="route.name === 'MyData' ? 'page' : undefined" aria-label="我的数据" title="我的数据" @click="handleMyDataClick">
        <FolderOpen class="size-[18px] shrink-0" />
        <span v-if="isLeftPanelShow" class="truncate text-sm">我的数据</span>
      </button>
      <button type="button" class="navigation-item" :class="[isLeftPanelShow ? '' : 'justify-center', route.name === 'SkillsLibrary' ? 'navigation-item-active' : '']" :aria-current="route.name === 'SkillsLibrary' ? 'page' : undefined" aria-label="分析技能库" title="分析技能库" @click="handleSkillsClick">
        <Puzzle class="size-[18px] shrink-0" />
        <span v-if="isLeftPanelShow" class="truncate text-sm">分析技能库</span>
      </button>
      <button type="button" class="navigation-item" :class="[isLeftPanelShow ? '' : 'justify-center', route.name === 'DataConnectors' ? 'navigation-item-active' : '']" :aria-current="route.name === 'DataConnectors' ? 'page' : undefined" aria-label="数据连接器" title="数据连接器" @click="handleConnectorsClick">
        <PlugZap class="size-[18px] shrink-0" />
        <span v-if="isLeftPanelShow" class="truncate text-sm">数据连接器</span>
      </button>
      <RouterLink to="/visualizations" class="navigation-item" :class="[isLeftPanelShow ? '' : 'justify-center', route.name === 'VisualizationSettings' ? 'navigation-item-active' : '']" :aria-current="route.name === 'VisualizationSettings' ? 'page' : undefined" aria-label="可视化设置" title="可视化设置">
        <ChartColumn class="size-[18px] shrink-0" />
        <span v-if="isLeftPanelShow" class="truncate text-sm">可视化设置</span>
      </RouterLink>
      <button v-if="isAdmin" type="button" class="navigation-item" :class="[isLeftPanelShow ? '' : 'justify-center', route.meta.adminOnly ? 'navigation-item-active' : '']" :aria-current="route.meta.adminOnly ? 'page' : undefined" aria-label="系统管理" title="系统管理" @click="handleAdminClick">
        <ShieldCheck class="size-[18px] shrink-0" />
        <span v-if="isLeftPanelShow" class="truncate text-sm">系统管理</span>
      </button>
    </div>

    <div class="mobile-safe-bottom relative shrink-0 border-t border-[var(--border-main)] p-2" ref="userMenuAnchorRef">
      <button type="button" class="navigation-item w-full" :class="isLeftPanelShow ? '' : 'justify-center px-0'" :aria-label="currentUser?.fullname ? `${currentUser.fullname}，账号菜单` : '账号菜单'" :aria-expanded="showUserMenu" aria-haspopup="dialog" @click="showUserMenu = !showUserMenu">
        <span class="flex size-7 shrink-0 items-center justify-center rounded-full bg-[#2b7659]/10 text-sm font-semibold text-[#2b7659] dark:text-[#a9cbbb]">{{ avatarLetter }}</span>
        <span v-if="isLeftPanelShow" class="min-w-0 flex-1 truncate text-sm text-left">{{ currentUser?.fullname || 'DataLab' }}</span>
      </button>
      <div v-if="showUserMenu" class="absolute z-50" :class="isLeftPanelShow ? 'bottom-full left-2 mb-2' : 'bottom-2 left-full ml-2'" @keydown.esc="showUserMenu = false">
        <UserMenu />
      </div>
    </div>
  </nav>
</template>

<script setup lang="ts">
import { ChartColumn, ChartNoAxesCombined, FolderOpen, PanelLeft, PlugZap, Puzzle, ShieldCheck, UserRound } from 'lucide-vue-next';
import ManusLogoTextIcon from './icons/ManusLogoTextIcon.vue';
import UserMenu from './UserMenu.vue';
import { useLeftPanel } from '../composables/useLeftPanel';
import { useAuth } from '../composables/useAuth';
import { useNewAnalysis } from '../composables/useNewAnalysis';
import { ref, computed, onMounted, onUnmounted, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

const { isLeftPanelShow, toggleLeftPanel } = useLeftPanel();
const { currentUser, isAdmin } = useAuth();
const { startNewAnalysis } = useNewAnalysis();
const route = useRoute();
const router = useRouter();
const showUserMenu = ref(false);
const userMenuAnchorRef = ref<HTMLElement | null>(null);
const avatarLetter = computed(() => currentUser.value?.fullname?.charAt(0)?.toUpperCase() || 'D');

const handleMyDataClick = () => router.push('/my-data');
const handleSkillsClick = () => router.push('/skills');
const handleConnectorsClick = () => router.push('/connectors');
const handleAdminClick = () => router.push('/chat/admin');
const closeMenuOutside = (event: PointerEvent) => {
  if (event.target instanceof Node && !userMenuAnchorRef.value?.contains(event.target)) showUserMenu.value = false;
};

watch(() => route.fullPath, () => { showUserMenu.value = false; });
onMounted(() => window.addEventListener('pointerdown', closeMenuOutside));
onUnmounted(() => window.removeEventListener('pointerdown', closeMenuOutside));
</script>

<style scoped>
.navigation-icon-button { display: flex; height: 36px; width: 36px; flex-shrink: 0; align-items: center; justify-content: center; border-radius: 6px; }
.navigation-item { display: flex; min-height: 40px; flex-shrink: 0; align-items: center; gap: 12px; padding: 8px 10px; border-radius: 8px; color: var(--text-secondary); text-align: left; transition: background-color 150ms; }
.navigation-item:hover, .navigation-icon-button:hover { background: var(--fill-tsp-white-light); }
.navigation-item-active { background: #2b76590d; color: #2b7659; }
.navigation-item:focus-visible, .navigation-icon-button:focus-visible { outline: 2px solid #2b7659; outline-offset: 2px; }
:global(.dark) .navigation-item-active { color: #a9cbbb; background: #a9cbbb12; }
</style>
