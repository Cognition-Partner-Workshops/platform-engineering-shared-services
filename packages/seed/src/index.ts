/**
 * TrustVault Seed Script
 * Populates demo data for Employment/Education Verification use case
 * Also sets up data for Loan Processing, Visa Application, and Farmer Insurance
 */

const TRUST_REGISTRY_URL = process.env.TRUST_REGISTRY_URL || 'http://localhost:3001';
const ISSUER_URL = process.env.ISSUER_SERVICE_URL || 'http://localhost:3002';
const VERIFIER_URL = process.env.VERIFIER_SERVICE_URL || 'http://localhost:3003';

async function post(url: string, body: unknown): Promise<Record<string, any>> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json() as Record<string, any>;
  if (!res.ok) {
    console.warn(`  WARN: ${url} -> ${res.status}`, data);
  }
  return data;
}

async function seed() {
  console.log('\n========================================');
  console.log('  TrustVault - Seed Script');
  console.log('========================================\n');

  // Step 1: Register credential schemas in Trust Registry
  console.log('1. Registering credential schemas...');

  const schemas = [
    {
      name: 'EmploymentCredential',
      version: '1.0',
      description: 'Proof of employment from an employer',
      schemaJson: {
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
        required: ['name', 'employer', 'position'],
      },
    },
    {
      name: 'EducationCredential',
      version: '1.0',
      description: 'Proof of education from a university or institution',
      schemaJson: {
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
        required: ['name', 'institution', 'degree'],
      },
    },
    {
      name: 'KYCCredential',
      version: '1.0',
      description: 'Know Your Customer identity verification',
      schemaJson: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          dateOfBirth: { type: 'string' },
          nationality: { type: 'string' },
          documentType: { type: 'string' },
          documentNumber: { type: 'string' },
          address: { type: 'string' },
        },
        required: ['name', 'documentType', 'documentNumber'],
      },
    },
    {
      name: 'IncomeCredential',
      version: '1.0',
      description: 'Proof of annual income from an employer or tax authority',
      schemaJson: {
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
    },
    {
      name: 'ProfessionalCertificationCredential',
      version: '1.0',
      description: 'Professional certification or license',
      schemaJson: {
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
        required: ['name', 'certificationName', 'issuingBody'],
      },
    },
  ];

  for (const s of schemas) {
    const result = await post(`${TRUST_REGISTRY_URL}/registry/schemas`, s);
    console.log(`  Schema "${s.name}": ${result.id ? 'OK' : 'exists/error'}`);
  }

  // Step 2: Register users for each service
  console.log('\n2. Registering users...');

  await post(`${TRUST_REGISTRY_URL}/auth/register`, { username: 'admin', password: 'admin123', role: 'admin' });
  console.log('  Trust Registry admin: admin/admin123');

  await post(`${ISSUER_URL}/auth/register`, { username: 'issuer', password: 'issuer123', role: 'issuer' });
  console.log('  Issuer Service user: issuer/issuer123');

  await post(`${VERIFIER_URL}/auth/register`, { username: 'verifier', password: 'verifier123', role: 'verifier' });
  console.log('  Verifier Service user: verifier/verifier123');

  // Step 3: Onboard issuers
  console.log('\n3. Onboarding issuers...');

  const issuersToOnboard = [
    {
      name: 'Acme Corporation',
      type: 'employer',
      authorizedSchemas: ['EmploymentCredential', 'IncomeCredential'],
    },
    {
      name: 'MIT University',
      type: 'university',
      authorizedSchemas: ['EducationCredential', 'ProfessionalCertificationCredential'],
    },
    {
      name: 'Government Identity Authority',
      type: 'government',
      authorizedSchemas: ['KYCCredential'],
    },
    {
      name: 'State Bank of India',
      type: 'bank',
      authorizedSchemas: ['IncomeCredential', 'KYCCredential'],
    },
    {
      name: 'National Farming Board',
      type: 'government',
      authorizedSchemas: ['EmploymentCredential', 'KYCCredential'],
    },
  ];

  const onboardedIssuers: Array<{ did: string; name: string }> = [];
  for (const iss of issuersToOnboard) {
    const result = await post(`${ISSUER_URL}/onboard`, iss);
    if (result.did) {
      onboardedIssuers.push({ did: result.did, name: iss.name });
      console.log(`  Issuer "${iss.name}": ${result.did.slice(0, 40)}...`);
    } else {
      console.log(`  Issuer "${iss.name}": FAILED`);
    }
  }

  // Step 4: Create trust policies for different use cases
  console.log('\n4. Creating trust policies...');

  const policies = [
    {
      name: 'Employment Verification Policy',
      acceptedSchemas: ['EmploymentCredential'],
      trustedRegistries: [TRUST_REGISTRY_URL],
      trustedIssuers: '*',
      checkRevocation: true,
      requireNonExpired: true,
    },
    {
      name: 'Education Verification Policy',
      acceptedSchemas: ['EducationCredential'],
      trustedRegistries: [TRUST_REGISTRY_URL],
      trustedIssuers: '*',
      checkRevocation: true,
      requireNonExpired: true,
    },
    {
      name: 'Employment + Education Verification',
      acceptedSchemas: ['EmploymentCredential', 'EducationCredential'],
      trustedRegistries: [TRUST_REGISTRY_URL],
      trustedIssuers: '*',
      checkRevocation: true,
      requireNonExpired: true,
    },
    {
      name: 'Loan Application Policy',
      acceptedSchemas: ['EmploymentCredential', 'IncomeCredential', 'KYCCredential'],
      trustedRegistries: [TRUST_REGISTRY_URL],
      trustedIssuers: '*',
      checkRevocation: true,
      requireNonExpired: true,
    },
    {
      name: 'Visa Application Policy',
      acceptedSchemas: ['EmploymentCredential', 'EducationCredential', 'KYCCredential'],
      trustedRegistries: [TRUST_REGISTRY_URL],
      trustedIssuers: '*',
      checkRevocation: true,
      requireNonExpired: true,
    },
    {
      name: 'Farmer Insurance Policy',
      acceptedSchemas: ['KYCCredential', 'EmploymentCredential'],
      trustedRegistries: [TRUST_REGISTRY_URL],
      trustedIssuers: '*',
      checkRevocation: true,
      requireNonExpired: true,
    },
  ];

  for (const p of policies) {
    const result = await post(`${VERIFIER_URL}/policies`, p);
    console.log(`  Policy "${p.name}": ${result.id ? result.id.slice(0, 8) + '...' : 'error'}`);
  }

  // Step 5: Issue sample credentials
  console.log('\n5. Issuing sample credentials...');

  if (onboardedIssuers.length >= 3) {
    const acmeDid = onboardedIssuers[0].did; // Acme Corporation
    const mitDid = onboardedIssuers[1].did;   // MIT University
    const govDid = onboardedIssuers[2].did;   // Government Identity Authority

    // Employment credential
    const empCred = await post(`${ISSUER_URL}/credentials`, {
      issuerDid: acmeDid,
      schemaName: 'EmploymentCredential',
      subjectDid: 'did:key:holder-placeholder',
      claims: {
        name: 'Ravi Kumar',
        employeeId: 'EMP-2024-001',
        employer: 'Acme Corporation',
        position: 'Senior Software Engineer',
        department: 'Engineering',
        startDate: '2022-03-15',
        employmentStatus: 'active',
      },
    });
    console.log(`  Employment Credential: ${empCred.credential?.id ? 'OK' : 'error'}`);

    // Education credential
    const eduCred = await post(`${ISSUER_URL}/credentials`, {
      issuerDid: mitDid,
      schemaName: 'EducationCredential',
      subjectDid: 'did:key:holder-placeholder',
      claims: {
        name: 'Ravi Kumar',
        institution: 'MIT University',
        degree: 'Master of Science',
        fieldOfStudy: 'Computer Science',
        graduationDate: '2021-06-15',
        gpa: '3.9',
        registrationNumber: 'MIT-2021-CS-4567',
      },
    });
    console.log(`  Education Credential: ${eduCred.credential?.id ? 'OK' : 'error'}`);

    // KYC credential
    const kycCred = await post(`${ISSUER_URL}/credentials`, {
      issuerDid: govDid,
      schemaName: 'KYCCredential',
      subjectDid: 'did:key:holder-placeholder',
      claims: {
        name: 'Ravi Kumar',
        dateOfBirth: '1995-08-20',
        nationality: 'Indian',
        documentType: 'Aadhaar',
        documentNumber: '1234-5678-9012',
        address: 'Mumbai, Maharashtra, India',
      },
    });
    console.log(`  KYC Credential: ${kycCred.credential?.id ? 'OK' : 'error'}`);

    // Income credential
    const incCred = await post(`${ISSUER_URL}/credentials`, {
      issuerDid: acmeDid,
      schemaName: 'IncomeCredential',
      subjectDid: 'did:key:holder-placeholder',
      claims: {
        name: 'Ravi Kumar',
        annualIncome: 2400000,
        currency: 'INR',
        employer: 'Acme Corporation',
        financialYear: '2025-2026',
      },
    });
    console.log(`  Income Credential: ${incCred.credential?.id ? 'OK' : 'error'}`);

    // Generate offers for each credential
    console.log('\n6. Generating credential offers...');
    const credIds = [empCred, eduCred, kycCred, incCred]
      .filter(c => c.credential?.id)
      .map(c => c.credential.id);

    for (const credId of credIds) {
      const id = credId.replace('urn:uuid:', '');
      const offer = await post(`${ISSUER_URL}/credentials/${id}/offer`, { requirePin: false });
      console.log(`  Offer for ${id.slice(0, 8)}...: ${offer.offerId ? offer.qrData : 'error'}`);
    }
  }

  console.log('\n========================================');
  console.log('  Seed complete!');
  console.log('========================================');
  console.log('\nDemo URLs:');
  console.log(`  Trust Registry:  ${TRUST_REGISTRY_URL}`);
  console.log(`  Issuer Service:  ${ISSUER_URL}`);
  console.log(`  Verifier Service: ${VERIFIER_URL}`);
  console.log(`  Wallet App:       http://localhost:5173`);
  console.log(`  Issuer Portal:    http://localhost:5174`);
  console.log(`  Verifier Portal:  http://localhost:5175`);
  console.log('\nDefault credentials:');
  console.log('  Trust Registry: admin / admin123');
  console.log('  Issuer Portal:  issuer / issuer123');
  console.log('  Verifier Portal: verifier / verifier123');
  console.log('');
}

seed().catch(err => {
  console.error('Seed failed:', err);
  process.exit(1);
});
