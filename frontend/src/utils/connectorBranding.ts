/** Official marks are bundled locally. Provenance: public/images/connectors/sources.json. */
import { batchConnectorLogos, darkBackgroundConnectorLogos } from './connectorBatchBranding';
import { expansionConnectorLogos } from './connectorExpansionBranding';
import { oct22ConnectorLogos } from './connectorOct22Branding';
import { oct23ConnectorLogos } from './connectorOct23Branding';
import { foundationConnectorLogos } from './connectorFoundationBranding';

const logos: Readonly<Record<string, string>> = {
  ...batchConnectorLogos,
  ...expansionConnectorLogos,
  ...oct22ConnectorLogos,
  ...oct23ConnectorLogos,
  ...foundationConnectorLogos,
  zenodo: 'zenodo.svg',
  datacite: 'datacite.svg',
  worldbank: 'worldbank.ico',
  'ncbi-datasets-v2': 'ncbi.ico',
  'ncbi-geo': 'ncbi.ico',
  'ncbi-sra': 'ncbi.ico',
  'harvard-dataverse': 'harvard-dataverse.png',
  openml: 'openml.ico',
  borealis: 'borealis.ico',
  'dataverse-nl': 'dataverse-nl.png',
  'recherche-data-gouv': 'recherche-data-gouv.png',
  pangaea: 'pangaea.ico',
  gbif: 'gbif.ico',
  sciencedb: 'sciencedb.ico',
};

export function connectorLogo(id: string): string | undefined {
  const filename = logos[id];
  return filename ? `${import.meta.env.BASE_URL}images/connectors/${filename}` : undefined;
}

export function connectorLogoNeedsDarkBackground(id: string): boolean {
  return darkBackgroundConnectorLogos.has(id);
}
