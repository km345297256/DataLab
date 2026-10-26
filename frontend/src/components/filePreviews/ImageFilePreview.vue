<template>
    <div class="relative flex flex-col items-center justify-center gap-3 flex-1 w-full min-h-0 p-3">
        <div v-if="loading" role="status" class="text-sm text-[var(--text-secondary)]">正在加载图片…</div>
        <div v-if="error" role="alert" class="max-w-lg text-center text-sm text-[var(--text-secondary)]">
            <p>{{ error }}</p>
            <button type="button" class="mt-3 rounded-lg border border-[var(--border-main)] px-3 py-1.5 hover:bg-[var(--fill-tsp-gray-main)]" @click="loadImage(file)">重试</button>
        </div>
        <img v-if="imageUrl" :key="imageUrl" :src="imageUrl" :alt="file.filename" class="w-full min-h-0 flex-1 object-contain" @load="imageLoaded" @error="imageFailed" />
        <p v-if="metadata && !error && !loading" class="shrink-0 text-xs text-[var(--text-tertiary)]">{{ metadata }}</p>
    </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { loadPluginBytes } from '../../visualizations/runtime';
import type { VisualizationPlugin } from '../../visualizations/contract';
import type { FileInfo } from '../../api/file';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { checkImageInput, inspectImage, ImagePreviewError } from '../../visualizations/imagePreflight';

const props = defineProps<{
    file: FileInfo;
    plugin: VisualizationPlugin;
}>();

const imageUrl = ref('');
const loading = ref(false);
const error = ref('');
const metadata = ref('');
const loads = usePreviewLoad();
let decodeTimer: ReturnType<typeof setTimeout> | undefined;
const clearDecodeTimer = () => { if (decodeTimer !== undefined) clearTimeout(decodeTimer); decodeTimer = undefined; };
const currentImageEvent = (event: Event) => (event.target as HTMLImageElement | null)?.src === imageUrl.value;
const imageLoaded = (event: Event) => { if (currentImageEvent(event)) { clearDecodeTimer(); loading.value = false; } };
const failDecode = () => {
    clearDecodeTimer(); loading.value = false;
    error.value = '浏览器未能解码这张图片，可能格式不兼容或文件损坏。可重试、转换为 PNG / JPEG，或下载查看。';
    if (imageUrl.value) URL.revokeObjectURL(imageUrl.value);
    imageUrl.value = '';
};
const imageFailed = (event: Event) => { if (currentImageEvent(event)) failDecode(); };

const loadImage = async (file: FileInfo) => {
    const load = loads.begin();
    imageUrl.value = ''; error.value = ''; metadata.value = ''; loading.value = true;
    if (!file) return;
    try {
        if (file.size != null) checkImageInput(file.size);
        const bytes = await loadPluginBytes(file, props.plugin, load.signal);
        load.assertCurrent();
        const header = inspectImage(bytes);
        const url = URL.createObjectURL(new Blob([bytes], { type: header.mime }));
        load.onDispose(() => URL.revokeObjectURL(url));
        imageUrl.value = url;
        metadata.value = `${header.width} × ${header.height}${header.frames > 1 ? ` · ${header.frames} 帧` : ''}`;
        decodeTimer = setTimeout(() => { if (load.isCurrent()) failDecode(); }, 20_000);
        load.onDispose(clearDecodeTimer);
    } catch (failure) {
        if (!load.isCurrent()) return;
        loading.value = false;
        error.value = failure instanceof ImagePreviewError ? failure.message : '图片加载失败，请重试；文件仍可下载查看。';
    }
};
watch(() => props.file, loadImage, { immediate: true });
</script>
