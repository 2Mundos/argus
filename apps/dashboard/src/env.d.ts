/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_NO_SIGNAL_MINUTES?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
