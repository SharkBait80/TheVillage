/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the Simulation_API, e.g. https://abc.execute-api.ap-southeast-2.amazonaws.com */
  readonly VITE_API_BASE_URL?: string
  /** Simulation identifier, e.g. "melb" */
  readonly VITE_SIM_ID?: string
  /** Set to "1" to run against the built-in fake state generator without a backend */
  readonly VITE_MOCK?: string
  /** Optional poll interval override in ms (default 1500) */
  readonly VITE_POLL_MS?: string
  /** Cognito region, e.g. "ap-southeast-2" */
  readonly VITE_COGNITO_REGION?: string
  /** Cognito App Client id (no client secret; USER_PASSWORD_AUTH enabled) */
  readonly VITE_COGNITO_CLIENT_ID?: string
  /** Optional operator username for automatic demo sign-in */
  readonly VITE_OPERATOR_USER?: string
  /** Optional operator password for automatic demo sign-in */
  readonly VITE_OPERATOR_PASS?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
