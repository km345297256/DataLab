import * as context from '../../src/visualizations/viewContext.ts';
import * as plot from '../../src/visualizations/plot.ts';
import * as interactions from '../../src/visualizations/plotlyInteractions.ts';
export const viewModules = Object.fromEntries(['./','../','../../'].flatMap(prefix => [
  [prefix+'viewContext', context], [prefix+'plot', plot], [prefix+'plotlyInteractions', interactions],
  [prefix+'ViewToolbar.vue', {default: {}}],
]));
