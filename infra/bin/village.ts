#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { VillageStack } from '../lib/village-stack';

const app = new cdk.App();

// Req 17.7 / 17.11 : environment must be exactly one of dev|test|prod
const ALLOWED_ENVS = ['dev', 'test', 'prod'] as const;
type EnvName = (typeof ALLOWED_ENVS)[number];

const envParam = (app.node.tryGetContext('env') as string | undefined) ?? 'dev';
if (!ALLOWED_ENVS.includes(envParam as EnvName)) {
  throw new Error(
    `Invalid context 'env' value '${envParam}'. Permitted values are: ${ALLOWED_ENVS.join(', ')}.`
  );
}
const environment = envParam as EnvName;

// A Simulation identifier used for tagging (Req 17.7); overridable via context.
const simulationId = (app.node.tryGetContext('simulationId') as string | undefined) ?? 'melb';
if (simulationId.length < 1 || simulationId.length > 64) {
  throw new Error('simulationId must be 1 to 64 characters (Req 17.7).');
}

const account = '490004615937';
const region = 'ap-southeast-2';

const stack = new VillageStack(app, `VillageStack-${environment}`, {
  env: { account, region },
  environment,
  simulationId,
  description: `Melbourne Agent Village infrastructure (${environment})`,
});

// Req 17.7: tag every provisioned resource.
cdk.Tags.of(stack).add('SimulationId', simulationId);
cdk.Tags.of(stack).add('Environment', environment);
