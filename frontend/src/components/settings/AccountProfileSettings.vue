<template>
  <section class="w-full space-y-6">
    <div class="rounded-lg border border-[var(--border-main)] p-4 text-sm">
      <p class="font-medium">{{ currentUser?.email }}</p>
      <p class="mt-2 text-[var(--text-tertiary)]">{{ currentUser?.role === 'admin' ? '管理员' : currentUser?.role === 'software' ? '服务账号' : '普通用户' }} · {{ currentUser?.is_active ? '账号已启用' : '账号已停用' }}</p>
      <p class="mt-2 leading-6 text-[var(--text-tertiary)]">你保存的数据、上传文件和分析记录归当前账号所有。公开分享的分析内容可通过分享链接访问；取消分享后链接失效。</p>
    </div>
    <form class="space-y-3" @submit.prevent="saveName">
      <h3 class="text-sm font-semibold">个人信息</h3>
      <label class="block text-sm" for="account-fullname">姓名</label>
      <input id="account-fullname" v-model="fullname" class="account-input" autocomplete="name" minlength="2" maxlength="100" required :disabled="saving" />
      <p v-if="profileError" role="alert" class="text-sm text-[var(--function-error)]">{{ profileError }}</p>
      <p v-if="profileNotice" role="status" class="text-sm text-[#2b7659]">{{ profileNotice }}</p>
      <button class="account-button" :disabled="saving || fullname.trim() === currentUser?.fullname">{{ saving ? '正在保存…' : '保存姓名' }}</button>
    </form>
    <form class="space-y-3 border-t border-[var(--border-main)] pt-5" @submit.prevent="savePassword">
      <h3 class="text-sm font-semibold">修改密码</h3>
      <p class="text-xs leading-5 text-[var(--text-tertiary)]">修改成功后，所有设备的登录会话将失效，请使用新密码重新登录。</p>
      <label class="block text-sm" for="account-current-password">当前密码</label>
      <input id="account-current-password" v-model="oldPassword" class="account-input" type="password" autocomplete="current-password" required :disabled="changingPassword" />
      <label class="block text-sm" for="account-new-password">新密码（至少 6 个字符）</label>
      <input id="account-new-password" v-model="newPassword" class="account-input" type="password" autocomplete="new-password" minlength="6" required :disabled="changingPassword" />
      <label class="block text-sm" for="account-confirm-password">确认新密码</label>
      <input id="account-confirm-password" v-model="confirmPassword" class="account-input" type="password" autocomplete="new-password" minlength="6" required :disabled="changingPassword" />
      <p v-if="passwordError" role="alert" class="text-sm text-[var(--function-error)]">{{ passwordError }}</p>
      <button class="account-button" :disabled="changingPassword">{{ changingPassword ? '正在修改…' : '修改密码并重新登录' }}</button>
    </form>
    <button type="button" class="text-sm text-[var(--function-error)] underline underline-offset-4" @click="logout()">退出当前账号</button>
  </section>
</template>
<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { changeFullname, changePassword } from '@/api/auth'
import { useAuth } from '@/composables/useAuth'
const { currentUser, loadCurrentUser, clearAuth, logout } = useAuth()
const fullname = ref(''), oldPassword = ref(''), newPassword = ref(''), confirmPassword = ref('')
const saving = ref(false), changingPassword = ref(false), profileError = ref(''), profileNotice = ref(''), passwordError = ref('')
let alive = true
watch(currentUser, user => { fullname.value = user?.fullname || '' }, { immediate: true })
onUnmounted(() => { alive = false; oldPassword.value = ''; newPassword.value = ''; confirmPassword.value = '' })
async function saveName() {
  if (saving.value) return
  profileError.value = ''; profileNotice.value = ''
  if (fullname.value.trim().length < 2 || fullname.value.trim().length > 100) { profileError.value = '姓名需要 2 至 100 个字符。'; return }
  saving.value = true
  try { await changeFullname({ fullname: fullname.value.trim() }); if (alive) { await loadCurrentUser(); profileNotice.value = '姓名已保存。' } }
  catch (cause) { if (alive) profileError.value = cause instanceof Error ? cause.message : '姓名更新失败，请重试。' }
  finally { saving.value = false }
}
async function savePassword() {
  if (changingPassword.value) return
  passwordError.value = ''
  if (!oldPassword.value || newPassword.value.length < 6) { passwordError.value = '请填写当前密码，新密码至少需要 6 个字符。'; return }
  if (newPassword.value !== confirmPassword.value) { passwordError.value = '两次输入的新密码不一致。'; return }
  if (newPassword.value === oldPassword.value) { passwordError.value = '新密码不能与当前密码相同。'; return }
  changingPassword.value = true
  try {
    await changePassword({ old_password: oldPassword.value, new_password: newPassword.value })
    if (alive) { clearAuth(); window.location.assign('/login?password_changed=1') }
  } catch (cause) { if (alive) passwordError.value = cause instanceof Error ? cause.message : '密码修改失败，请重试。' }
  finally { changingPassword.value = false }
}
</script>
<style scoped>
.account-input { display: block; width: 100%; border: 1px solid var(--border-main); border-radius: 8px; padding: 10px 12px; background: var(--fill-input-chat); font-size: 14px; }
.account-button { border-radius: 8px; padding: 9px 14px; background: var(--Button-primary-black); color: var(--text-onblack); font-size: 13px; }
.account-button:disabled { opacity: .45; cursor: not-allowed; }
.account-input:focus-visible, .account-button:focus-visible { outline: 2px solid #2b7659; outline-offset: 2px; }
</style>
