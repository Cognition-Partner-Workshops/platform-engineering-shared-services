// Types
export * from './types.js';

// DID utilities
export {
  generateES256KeyPair,
  deriveDidKey,
  extractPublicKeyFromDidKey,
  buildDidDocument,
  importPrivateKey,
  importPublicKey,
} from './did.js';

// Verifiable Credential utilities
export {
  createVerifiableCredential,
  verifyCredentialJwt,
  decodeCredentialJwt,
} from './vc.js';

// Verifiable Presentation utilities
export {
  createVerifiablePresentation,
  verifyPresentationJwt,
  decodePresentationJwt,
} from './vp.js';

// Status List
export { StatusList, allocateStatusIndex, resetStatusIndex } from './status-list.js';

// Auth
export { generateAuthToken, verifyAuthToken, hashPassword } from './auth.js';
