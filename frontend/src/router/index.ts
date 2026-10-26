import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { installFilePanelRouteLifecycle } from '@/composables/useFilePanel'
import { useAuth } from '@/composables/useAuth'

const MainLayout = () => import('@/pages/MainLayout.vue')
const HomePage = () => import('@/pages/HomePage.vue')
const PersonalCenterPage = () => import('@/pages/PersonalCenterPage.vue')
const ChatPage = () => import('@/pages/ChatPage.vue')
const ShareLayout = () => import('@/pages/ShareLayout.vue')
const SharePage = () => import('@/pages/SharePage.vue')
const PluginsPage = () => import('@/pages/PluginsPage.vue')
const VisualizationSettingsPage = () => import('@/pages/VisualizationSettingsPage.vue')
const AdminPage = () => import('@/pages/AdminPage.vue')

const routes: RouteRecordRaw[] = [
  {
    path: '/chat',
    component: MainLayout,
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        alias: ['/', '/home'],
        component: HomePage,
        name: 'NewAnalysis',
        meta: { analysisWorkspace: true },
      },
      {
        path: '/account',
        name: 'PersonalCenter',
        component: PersonalCenterPage,
        meta: { personalCenter: true },
      },
      {
        path: 'my-data',
        alias: '/my-data',
        name: 'MyData',
        meta: { personalCenter: true },
        component: () => import('@/pages/MyDataPage.vue'),
      },
      {
        path: 'skills',
        alias: '/skills',
        name: 'SkillsLibrary',
        meta: { catalogType: 'skills', personalCenter: true },
        component: PluginsPage,
      },
      {
        path: 'connectors',
        alias: '/connectors',
        name: 'DataConnectors',
        meta: { catalogType: 'connectors', personalCenter: true },
        component: PluginsPage,
      },
      {
        path: 'visualizations',
        alias: '/visualizations',
        name: 'VisualizationSettings',
        meta: { personalCenter: true },
        component: VisualizationSettingsPage,
      },
      {
        path: 'plugins',
        alias: '/plugins',
        redirect: to => ({ path: to.query.tab === 'renderers' ? '/visualizations' : to.query.tab === 'connectors' ? '/connectors' : '/skills', query: {} }),
      },
      {
        path: 'admin',
        component: AdminPage,
        meta: { adminOnly: true, personalCenter: true },
      },
      {
        path: 'admin/tasks/:sessionId/replay',
        component: SharePage,
        meta: { adminReplay: true, adminOnly: true, personalCenter: true },
      },
      {
        path: ':sessionId',
        component: ChatPage,
        name: 'AnalysisTask',
        meta: { analysisWorkspace: true },
      },
    ],
  },
  {
    path: '/share',
    component: ShareLayout,
    children: [
      {
        path: ':sessionId',
        component: SharePage,
      },
    ],
  },
  {
    path: '/login',
    component: () => import('@/pages/LoginPage.vue'),
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuth()
  await auth.initAuth()
  if (to.matched.some(record => record.meta.requiresAuth) && !auth.isAuthenticated.value) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.matched.some(record => record.meta.adminOnly) && !auth.isAdmin.value) return '/'
  if (to.path === '/login' && auth.isAuthenticated.value) return '/'
})

if (typeof window !== 'undefined') {
  window.addEventListener('datalab:auth-expired', () => {
    const path = router.currentRoute.value.fullPath
    if (router.currentRoute.value.matched.some(record => record.meta.requiresAuth)) {
      void router.replace({ path: '/login', query: { redirect: path } })
    }
  })
}

installFilePanelRouteLifecycle(router)

export default router
