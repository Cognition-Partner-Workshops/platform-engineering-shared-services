import { z } from 'zod';

// ==================== Auth ====================
export const LoginSchema = z.object({
  username: z.string().min(1),
  password: z.string().min(4),
});

export const RegisterSchema = z.object({
  username: z.string().min(1),
  password: z.string().min(4),
  role: z.enum(['admin', 'issuer', 'verifier']).default('admin'),
});

export type LoginInput = z.infer<typeof LoginSchema>;
export type RegisterInput = z.infer<typeof RegisterSchema>;

export interface AuthUser {
  id: string;
  username: string;
  role: string;
}

export interface JWTPayload {
  sub: string;
  username: string;
  role: string;
  iat: number;
  exp: number;
}

// ==================== Trust Registry ====================
export const IssuerSchema = z.object({
  did: z.string(),
  name: z.string(),
  type: z.enum(['government', 'enterprise', 'university', 'bank', 'employer', 'other']),
  authorizedSchemas: z.array(z.string()),
  status: z.enum(['active', 'suspended', 'revoked']).default('active'),
});

export const CreateIssuerSchema = z.object({
  did: z.string(),
  name: z.string(),
  type: z.enum(['government', 'enterprise', 'university', 'bank', 'employer', 'other']),
  authorizedSchemas: z.array(z.string()),
  status: z.enum(['active', 'suspended', 'revoked']).default('active'),
});

export const CredentialSchemaRegistration = z.object({
  name: z.string(),
  version: z.string().default('1.0'),
  description: z.string().optional(),
  schemaJson: z.record(z.unknown()),
});

export type Issuer = z.infer<typeof IssuerSchema>;
export type CreateIssuerInput = z.infer<typeof CreateIssuerSchema>;
export type CredentialSchemaInput = z.infer<typeof CredentialSchemaRegistration>;

// ==================== Issuer Service ====================
export const OnboardIssuerSchema = z.object({
  name: z.string(),
  type: z.enum(['government', 'enterprise', 'university', 'bank', 'employer', 'other']),
  authorizedSchemas: z.array(z.string()),
});

export const CreateCredentialSchema = z.object({
  schemaName: z.string(),
  subjectDid: z.string(),
  claims: z.record(z.unknown()),
  expirationDate: z.string().optional(),
});

export const CreateOfferSchema = z.object({
  requirePin: z.boolean().default(false),
});

export type OnboardIssuerInput = z.infer<typeof OnboardIssuerSchema>;
export type CreateCredentialInput = z.infer<typeof CreateCredentialSchema>;

// ==================== Verifier Service ====================
export const CreatePolicySchema = z.object({
  name: z.string(),
  acceptedSchemas: z.array(z.string()),
  trustedRegistries: z.array(z.string()),
  trustedIssuers: z.union([z.literal('*'), z.array(z.string())]),
  checkRevocation: z.boolean().default(true),
  requireNonExpired: z.boolean().default(true),
});

export const CreatePresentationRequestSchema = z.object({
  policyId: z.string(),
  purpose: z.string().optional(),
});

export type CreatePolicyInput = z.infer<typeof CreatePolicySchema>;
export type CreatePresentationRequestInput = z.infer<typeof CreatePresentationRequestSchema>;

// ==================== Credential Types ====================
export interface VerifiableCredential {
  '@context': string[];
  type: string[];
  id: string;
  issuer: string;
  issuanceDate: string;
  expirationDate?: string;
  credentialSubject: {
    id: string;
    [key: string]: unknown;
  };
  credentialStatus?: {
    id: string;
    type: string;
    statusPurpose: string;
    statusListIndex: string;
    statusListCredential: string;
  };
  proof?: {
    type: string;
    jwt: string;
  };
}

export interface VerifiablePresentation {
  '@context': string[];
  type: string[];
  holder: string;
  verifiableCredential: VerifiableCredential[];
  proof?: {
    type: string;
    jwt: string;
  };
}

// ==================== Verification ====================
export interface VerificationCheck {
  checkName: string;
  passed: boolean | null;
  detail: string;
  timestamp: string | null;
}

export interface VerificationResult {
  sessionId: string;
  decision: 'accepted' | 'rejected';
  checks: VerificationCheck[];
  holderDid?: string;
  policyName?: string;
  credentialsVerified?: number;
}

// ==================== Status List ====================
export interface StatusListEntry {
  id: string;
  type: string;
  statusPurpose: string;
  statusListIndex: string;
  statusListCredential: string;
}

// ==================== Predefined Credential Schemas ====================
export const CREDENTIAL_SCHEMAS: Record<string, { type: string; properties: Record<string, { type: string }>; required: string[] }> = {
  EmploymentCredential: {
    type: 'object',
    properties: {
      name: { type: 'string' },
      employeeId: { type: 'string' },
      employer: { type: 'string' },
      position: { type: 'string' },
      department: { type: 'string' },
      startDate: { type: 'string' },
      employmentStatus: { type: 'string' },
    },
    required: ['name', 'employer', 'position', 'employmentStatus'],
  },
  EducationCredential: {
    type: 'object',
    properties: {
      name: { type: 'string' },
      institution: { type: 'string' },
      degree: { type: 'string' },
      fieldOfStudy: { type: 'string' },
      graduationDate: { type: 'string' },
      gpa: { type: 'string' },
      registrationNumber: { type: 'string' },
    },
    required: ['name', 'institution', 'degree', 'fieldOfStudy'],
  },
  KYCCredential: {
    type: 'object',
    properties: {
      name: { type: 'string' },
      dateOfBirth: { type: 'string' },
      nationality: { type: 'string' },
      documentType: { type: 'string' },
      documentNumber: { type: 'string' },
      address: { type: 'string' },
    },
    required: ['name', 'dateOfBirth', 'nationality', 'documentNumber'],
  },
  IncomeCredential: {
    type: 'object',
    properties: {
      name: { type: 'string' },
      annualIncome: { type: 'number' },
      currency: { type: 'string' },
      employer: { type: 'string' },
      financialYear: { type: 'string' },
    },
    required: ['name', 'annualIncome', 'currency'],
  },
  ProfessionalCertificationCredential: {
    type: 'object',
    properties: {
      name: { type: 'string' },
      certificationName: { type: 'string' },
      issuingBody: { type: 'string' },
      certificationId: { type: 'string' },
      issueDate: { type: 'string' },
      expiryDate: { type: 'string' },
      domain: { type: 'string' },
    },
    required: ['name', 'certificationName', 'issuingBody', 'certificationId'],
  },
};
