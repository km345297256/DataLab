<template>
  <section class="rounded-2xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-5">
    <div class="mb-5 flex flex-wrap items-start justify-between gap-4">
      <div><h2 class="text-lg font-semibold">用户与权限</h2><p class="mt-1 text-sm text-[var(--text-tertiary)]">审核科研人员账号，管理角色与账号状态。用户的数据和分析记录按账号独立保存。</p></div>
      <button class="user-button" :disabled="loading || busy !== null" @click="load">刷新</button>
    </div>
    <form class="mb-5 flex flex-wrap items-end gap-3" @submit.prevent="applyFilters">
      <label class="flex min-w-[180px] flex-1 flex-col gap-1.5 text-sm">姓名或邮箱<input v-model="query" type="search" maxlength="200" placeholder="搜索用户" class="user-input" /></label>
      <label class="flex flex-col gap-1.5 text-sm">账号状态<select v-model="status" class="user-input"><option value="">全部状态</option><option value="pending">待审核</option><option value="active">已启用</option><option value="inactive">已停用</option><option value="rejected">已拒绝</option></select></label>
      <label class="flex flex-col gap-1.5 text-sm">角色<select v-model="role" class="user-input"><option value="">全部角色</option><option value="user">普通用户</option><option value="admin">管理员</option><option value="software">服务账号</option></select></label>
      <button type="submit" class="user-button" :disabled="busy !== null">查询</button>
    </form>
    <p v-if="error" role="alert" class="mb-3 text-sm text-[var(--function-error)]">{{ error }}</p>
    <p v-if="notice" role="status" class="mb-3 text-sm text-[#2b7659] dark:text-[#a9cbbb]">{{ notice }}</p>
    <p v-if="loading" class="py-8 text-center text-sm" role="status">正在加载用户…</p>
    <div v-else class="overflow-x-auto">
      <table class="w-full min-w-[680px] text-left text-sm">
        <caption class="sr-only">用户账号、角色、状态和管理操作</caption>
        <thead class="text-[var(--text-tertiary)]"><tr><th scope="col" class="py-3">姓名 / 邮箱</th><th scope="col">角色</th><th scope="col">状态</th><th scope="col">最近登录</th><th scope="col">操作</th></tr></thead>
        <tbody><tr v-for="user in users" :key="user.id" class="border-t border-[var(--border-main)]">
          <td class="py-4 pr-3"><p>{{ user.fullname }}<span v-if="user.id === currentUser?.id" class="ml-1 text-xs text-[var(--text-tertiary)]">（当前账号）</span></p><p class="mt-1 text-xs text-[var(--text-tertiary)]">{{ user.email }}</p></td>
          <td class="pr-3">{{ roleLabels[user.role] }}</td>
          <td class="pr-3">{{ accountStatus(user) }}</td>
          <td class="pr-3 text-xs text-[var(--text-tertiary)]">{{ displayDate(user.last_login_at) }}</td>
          <td class="py-3">
            <div v-if="user.registration_status === 'pending'" class="flex gap-2">
              <button :disabled="busy !== null" class="user-button" @click="prepare(user, 'approve')">批准</button>
              <button :disabled="busy !== null" class="user-button" @click="prepare(user, 'reject')">拒绝</button>
            </div>
            <div v-else-if="user.registration_status === 'approved' && user.id !== currentUser?.id" class="flex gap-2">
              <button :disabled="busy !== null" class="user-button" @click="prepare(user, user.is_active ? 'deactivate' : 'activate')">{{ user.is_active ? '停用' : '启用' }}</button>
              <button :disabled="busy !== null" class="user-button" @click="prepare(user, 'role')">修改角色</button>
            </div>
            <span v-else class="text-xs text-[var(--text-tertiary)]">{{ user.id === currentUser?.id ? '当前账号不可自行降权或停用' : '注册申请已拒绝' }}</span>
          </td>
        </tr></tbody>
      </table>
      <p v-if="!users.length" class="py-8 text-center text-sm text-[var(--text-tertiary)]">没有符合条件的用户</p>
    </div>
    <div class="mt-4 flex items-center justify-between gap-3 text-sm">
      <span class="text-[var(--text-tertiary)]">共 {{ total }} 个账号</span>
      <div class="flex items-center gap-3"><button :disabled="offset === 0 || loading || busy !== null" class="user-button" @click="goPage(-1)">上一页</button><span>第 {{ offset / pageSize + 1 }} 页</span><button :disabled="offset + pageSize >= total || loading || busy !== null" class="user-button" @click="goPage(1)">下一页</button></div>
    </div>
    <div class="mt-5 rounded-lg bg-[var(--fill-tsp-gray-main)] p-3 text-xs leading-6 text-[var(--text-tertiary)]">
      普通用户可检索开放数据、管理自己的数据和分析；管理员另外可审核账号、管理系统配置。服务账号用于接口集成，不具备管理权限。变更角色或停用账号后，原登录会话将失效。
    </div>
  </section>
  <Dialog :open="pending !== null" @update:open="closeConfirmation">
    <DialogContent class="max-w-md">
      <DialogHeader><DialogTitle>{{ confirmationTitle }}</DialogTitle><DialogDescription>{{ pending?.user.fullname }} · {{ pending?.user.email }}</DialogDescription></DialogHeader>
      <label v-if="pending?.action === 'role'" class="flex flex-col gap-2 text-sm">新角色<select v-model="selectedRole" class="user-input"><option value="user">普通用户</option><option value="admin">管理员</option><option value="software">服务账号</option></select></label>
      <p class="text-sm leading-6 text-[var(--text-secondary)]">{{ confirmationDescription }}</p>
      <p v-if="actionError" role="alert" class="text-sm text-[var(--function-error)]">{{ actionError }}</p>
      <DialogFooter><button class="user-button" :disabled="busy !== null" @click="closeConfirmation(false)">取消</button><button class="user-button bg-[var(--Button-primary-black)] text-[var(--text-onblack)]" :disabled="busy !== null || (pending?.action === 'role' && selectedRole === pending.user.role)" @click="confirmAction">{{ busy ? '正在处理…' : '确认' }}</button></DialogFooter>
    </DialogContent>
  </Dialog>
</template>
<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { listUsers, updateUser, decideRegistration, type User, type UserRole, type UserStatusFilter, type UserListQuery } from '@/api/auth'
import { useAuth } from '@/composables/useAuth'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'

type Action = 'approve' | 'reject' | 'activate' | 'deactivate' | 'role'
const { currentUser } = useAuth()
const users = ref<User[]>([]), total = ref(0), loading = ref(false), error = ref(''), notice = ref(''), actionError = ref('')
const busy = ref<string | null>(null), offset = ref(0), query = ref(''), status = ref<UserStatusFilter | ''>(''), role = ref<UserRole | ''>('')
const pending = ref<{ user: User; action: Action } | null>(null), selectedRole = ref<UserRole>('user')
const pageSize = 25
const roleLabels = { admin: '管理员', user: '普通用户', software: '服务账号' }
let filters: UserListQuery = {}, revision = 0, controller: AbortController | undefined, mounted = true
const confirmationTitle = computed(() => ({ approve: '批准注册申请', reject: '拒绝注册申请', activate: '启用账号', deactivate: '停用账号', role: '修改用户角色' })[pending.value?.action || 'role'])
const confirmationDescription = computed(() => {
  if (pending.value?.action === 'approve') return '批准后，该用户可登录并使用独立的数据与分析空间。'
  if (pending.value?.action === 'reject') return '拒绝后，该账号无法登录。请确认该申请不符合使用要求。'
  if (pending.value?.action === 'activate') return '启用后，该用户可以重新登录。原有数据和分析记录继续保留。'
  if (pending.value?.action === 'deactivate') return '停用后，该用户将无法继续访问账号，现有登录会话失效。数据和分析记录继续保留。'
  return selectedRole.value === 'admin' ? '管理员可以审核其他账号并管理系统配置。确认授予该用户管理权限？' : '更改角色后，该用户需要重新登录，原有数据和分析记录继续保留。'
})
function accountStatus(user: User) { return user.registration_status === 'pending' ? '待审核' : user.registration_status === 'rejected' ? '已拒绝' : user.is_active ? '已启用' : '已停用' }
function displayDate(value?: string) { const date = value ? new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`) : null; return date && Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', { hour12: false }) : '尚未登录' }
async function load() {
  const version = ++revision
  controller?.abort(); controller = new AbortController()
  loading.value = true; error.value = ''
  try {
    const result = await listUsers({ ...filters, limit: pageSize, offset: offset.value }, controller.signal)
    if (!mounted || version !== revision) return
    users.value = result.users; total.value = result.total
  } catch (cause) {
    if (mounted && version === revision) { users.value = []; error.value = cause instanceof Error ? cause.message : '用户列表加载失败，请重试。' }
  } finally { if (mounted && version === revision) loading.value = false }
}
function applyFilters() { filters = { query: query.value.trim() || undefined, role: role.value || undefined, status: status.value || undefined }; offset.value = 0; void load() }
function goPage(direction: number) { offset.value = Math.max(0, offset.value + direction * pageSize); void load() }
function prepare(user: User, action: Action) { pending.value = { user, action }; selectedRole.value = user.role; actionError.value = ''; notice.value = '' }
function closeConfirmation(open: boolean) { if (!open && !busy.value) { pending.value = null; actionError.value = '' } }
async function confirmAction() {
  const operation = pending.value
  if (!operation || busy.value) return
  busy.value = operation.user.id; actionError.value = ''
  try {
    if (operation.action === 'approve' || operation.action === 'reject') await decideRegistration(operation.user.id, operation.action === 'approve' ? 'approved' : 'rejected')
    else await updateUser(operation.user.id, operation.action === 'role' ? { role: selectedRole.value } : { is_active: operation.action === 'activate' })
    if (!mounted) return
    pending.value = null; notice.value = '用户状态已更新。'; await load()
  } catch (cause) { if (mounted) actionError.value = cause instanceof Error ? cause.message : '操作未完成，请刷新确认账号状态后重试。' }
  finally { busy.value = null }
}
onMounted(load)
onUnmounted(() => { mounted = false; ++revision; controller?.abort() })
</script>
<style scoped>
.user-input { min-height: 38px; border: 1px solid var(--border-main); border-radius: 8px; padding: 8px 10px; background: var(--background-gray-main); color: var(--text-primary); }
.user-button { border: 1px solid var(--border-main); border-radius: 8px; padding: 7px 12px; font-size: 13px; white-space: nowrap; }
.user-button:disabled { opacity: .45; cursor: not-allowed; }
.user-button:not(:disabled):hover { filter: brightness(.95); }
.user-button:focus-visible, .user-input:focus-visible { outline: 2px solid #2b7659; outline-offset: 2px; }
</style>
