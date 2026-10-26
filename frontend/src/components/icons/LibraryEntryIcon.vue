<template>
  <svg :width="size" :height="size" viewBox="0 0 72 72" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false" class="library-entry-icon">
    <template v-if="kind === 'folder'">
      <path d="M8 24a6 6 0 0 1 6-6h13.5a5 5 0 0 1 3.6 1.5l3.7 3.8H58a6 6 0 0 1 6 6v26a6 6 0 0 1-6 6H14a6 6 0 0 1-6-6V24Z" fill="#634500" fill-opacity=".08" />
      <path d="M8 21a6 6 0 0 1 6-6h12.8a5 5 0 0 1 3.5 1.4l4.4 4.3H58a6 6 0 0 1 6 6V53a6 6 0 0 1-6 6H14a6 6 0 0 1-6-6V21Z" fill="#DDA126" />
      <path d="M8 21a6 6 0 0 1 6-6h12.8a5 5 0 0 1 3.5 1.4l4.4 4.3H58a6 6 0 0 1 6 6v1.8H8V21Z" fill="#F0BA45" />
      <path d="M14 24.5h43.5a3.5 3.5 0 0 1 3.5 3.5v23H11V27.5a3 3 0 0 1 3-3Z" fill="#FFE4A0" />
      <path d="M13.6 29h44.8a5.4 5.4 0 0 1 5.4 5.9l-1.7 19.6a5 5 0 0 1-5 4.5H14.9a5 5 0 0 1-5-4.5L8.2 34.9a5.4 5.4 0 0 1 5.4-5.9Z" fill="#F7C74C" />
      <path d="M13.6 29h44.8a5.4 5.4 0 0 1 5.2 4H8.4a5.4 5.4 0 0 1 5.2-4Z" fill="#FFDA70" />
      <path d="M10.3 52.5h51.4l-.2 2a4.3 4.3 0 0 1-4.3 3.9H14.8a4.3 4.3 0 0 1-4.3-3.9l-.2-2Z" fill="#ECB83B" fill-opacity=".55" />
      <path d="M15 16h11.2a4 4 0 0 1 2.8 1.1l3.2 3.2" stroke="#FFE49B" stroke-width="1.2" stroke-linecap="round" />
    </template>
    <template v-else>
      <path d="M21 9h22.5L59 24.5V59a6 6 0 0 1-6 6H21a6 6 0 0 1-6-6V15a6 6 0 0 1 6-6Z" fill="#17213A" fill-opacity=".06" />
      <path d="M21 7.5h22.3L57.5 22V58a5.5 5.5 0 0 1-5.5 5.5H21a5.5 5.5 0 0 1-5.5-5.5V13A5.5 5.5 0 0 1 21 7.5Z" fill="#FCFDFE" stroke="#D6DDE7" />
      <path d="M43 8v10a5 5 0 0 0 5 5h9" stroke="#D6DDE7" stroke-linejoin="round" />
      <path d="M43.6 8.6v9.1a4.7 4.7 0 0 0 4.7 4.7h8.4L43.6 8.6Z" fill="#EAF0F6" />

      <g v-if="category === 'spreadsheet'" :stroke="color" stroke-width="1.3" stroke-linejoin="round">
        <rect x="25" y="27" width="23" height="15" rx="2" :fill="tint" />
        <path d="M25.5 32h22M25.5 37h22M32.5 27.5v14M40.2 27.5v14" />
      </g>
      <g v-else-if="category === 'research'" :stroke="color" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M25 39.5h23M26.5 28v11.5" stroke-opacity=".35" />
        <path d="m27 36 5-5 5.5 4 5.5-7 4 3" />
        <circle cx="32" cy="31" r="1.9" :fill="color" stroke="#FCFDFE" stroke-width="1" />
        <circle cx="37.5" cy="35" r="1.9" :fill="color" stroke="#FCFDFE" stroke-width="1" />
        <circle cx="43" cy="28" r="1.9" :fill="color" stroke="#FCFDFE" stroke-width="1" />
      </g>
      <g v-else-if="category === 'image'">
        <rect x="25" y="26.5" width="23" height="17" rx="2.5" :fill="tint" :stroke="color" stroke-width="1.2" />
        <circle cx="41.8" cy="31.2" r="2.1" :fill="color" fill-opacity=".8" />
        <path d="m26.7 40 6.2-7.1 4.7 5 3.2-3 5.5 6.6H26.7V40Z" :fill="color" fill-opacity=".75" />
      </g>
      <g v-else-if="category === 'code'" :stroke="color" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <path d="m30.5 29-5.5 5 5.5 5m12-10 5.5 5-5.5 5M38.5 27l-4 14" />
      </g>
      <g v-else-if="category === 'pdf'" :stroke="color" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        <path d="M29 40c5.5-7.5 7.1-13.9 5.5-14.4-2.1-.6-1.2 7.2 3.9 11.4 2.8 2.3 6.8 3.3 7.2 1.4.5-2.6-11.2-2.4-16.6 1.6Z" />
      </g>
      <g v-else-if="category === 'document'" :stroke="color" stroke-width="1.6" stroke-linecap="round">
        <path d="M26 28h20M26 32.5h20M26 37h20M26 41.5h12" />
        <path d="M22.5 28v13.5" stroke-width="2.3" stroke-opacity=".5" />
      </g>
      <g v-else stroke="#9AA6B6" stroke-width="1.5" stroke-linecap="round">
        <path d="M25 28h16M25 33h22M25 38h22M25 43h12" />
      </g>

      <rect x="9" y="46.5" width="37" height="16" rx="4" fill="#17213A" fill-opacity=".05" />
      <rect x="9" y="45" width="37" height="16" rx="4" :fill="color" />
      <path d="M13 45.7h29" stroke="white" stroke-opacity=".18" stroke-linecap="round" />
      <text x="27.5" y="55.8" fill="white" :font-size="label.length > 4 ? 7.5 : 8.5" font-family="Segoe UI, Arial, sans-serif" font-weight="700" text-anchor="middle" letter-spacing=".35">{{ label }}</text>
    </template>
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = withDefaults(defineProps<{ kind?: 'folder' | 'file'; filename?: string; size?: number }>(), {
  kind: 'file', filename: '', size: 64,
});

const extension = computed(() => {
  const name = props.filename.replace(/\\/g, '/').split('/').pop() ?? '';
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
});
const category = computed(() => {
  const ext = extension.value;
  if (ext === 'pdf') return 'pdf';
  if (['doc', 'docx', 'odt', 'rtf'].includes(ext)) return 'document';
  if (['csv', 'tsv', 'xls', 'xlsx', 'ods'].includes(ext)) return 'spreadsheet';
  if (['nc', 'netcdf', 'h5', 'hdf', 'hdf4', 'hdf5', 'mat', 'parquet', 'feather', 'fits', 'fit', 'grib', 'grib2', 'mseed', 'sac'].includes(ext)) return 'research';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'tif', 'tiff', 'heic', 'avif'].includes(ext)) return 'image';
  if (['json', 'jsonl', 'py', 'ipynb', 'js', 'ts', 'r', 'sql', 'xml', 'yaml', 'yml', 'toml', 'sh'].includes(ext)) return 'code';
  return 'generic';
});
const palettes = {
  pdf: { color: '#D65052', tint: '#FBE9E9' },
  document: { color: '#3474C5', tint: '#E8F0FB' },
  spreadsheet: { color: '#26855E', tint: '#E6F3EB' },
  research: { color: '#2486A6', tint: '#E6F3F8' },
  image: { color: '#8B61B5', tint: '#F0EAF7' },
  code: { color: '#756587', tint: '#F0EDF5' },
  generic: { color: '#7B8796', tint: '#EFF2F5' },
};
const color = computed(() => palettes[category.value].color);
const tint = computed(() => palettes[category.value].tint);
const label = computed(() => {
  const ext = extension.value;
  if (ext === 'netcdf') return 'NC';
  if (ext === 'parquet') return 'PARQ';
  if (ext === 'feather') return 'DATA';
  if (ext === 'jpeg') return 'JPG';
  return /^[a-z0-9]{1,5}$/.test(ext) ? ext.toUpperCase() : category.value === 'image' ? 'IMG' : 'FILE';
});
</script>

<style scoped>
.library-entry-icon { display: block; flex: none; overflow: visible; }
</style>
