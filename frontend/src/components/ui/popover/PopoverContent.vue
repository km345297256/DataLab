<script setup lang="ts">
import type { PopoverContentEmits, PopoverContentProps } from "reka-ui"
import type { HTMLAttributes } from "vue"
import { reactiveOmit } from "@vueuse/core"
import {
  PopoverContent,

  PopoverPortal,
  useForwardPropsEmits,
} from "reka-ui"
import { cn } from "@/lib/utils"

defineOptions({
  inheritAttrs: false,
})

const props = withDefaults(
  defineProps<PopoverContentProps & { class?: HTMLAttributes["class"] }>(),
  {
    align: "center",
    sideOffset: 4,
  },
)
const emits = defineEmits<PopoverContentEmits>()

const delegatedProps = reactiveOmit(props, "class")

const forwarded = useForwardPropsEmits(delegatedProps, emits)
</script>

<template>
  <PopoverPortal>
    <PopoverContent
      data-slot="popover-content"
      v-bind="{ ...forwarded, ...$attrs }"
      :class="
        cn(
          'outline-none w-[400px] max-w-[calc(100vw-24px)] flex flex-col rounded-2xl bg-[var(--background-menu-white)] border border-[var(--border-light)] shadow-[0px_8px_32px_0px_var(--shadow-S)]',
          props.class,
        )
      "
    >
      <slot />
    </PopoverContent>
  </PopoverPortal>
</template>
