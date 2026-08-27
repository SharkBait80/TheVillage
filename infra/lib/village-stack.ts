import * as path from 'path';
import * as fs from 'fs';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as apigwv2Integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as apigwv2Authorizers from 'aws-cdk-lib/aws-apigatewayv2-authorizers';

/**
 * Props for {@link VillageStack}.
 *
 * `environment` is validated to `dev | test | prod` by bin/village.ts (Req 17.11).
 * `simulationId` (1-64 chars) is used for resource naming and tagging (Req 17.7).
 */
export interface VillageStackProps extends cdk.StackProps {
  readonly environment: 'dev' | 'test' | 'prod';
  readonly simulationId: string;
}

/**
 * Melbourne Agent Village — single-account, single-region (ap-southeast-2)
 * infrastructure (Requirement 17). All compute, DynamoDB, AgentCore, API and
 * SPA hosting live in ap-southeast-2; only Stable Diffusion image generation
 * targets us-west-2 and is passed to the Asset Generator lambda as an env var
 * so no cross-region resource is provisioned by this stack.
 *
 * IAM (Req 17.6 / DESIGN.md §9): four purpose-specific principals
 * (ApiLambdaRole, AssetLambdaRole, EngineTaskRole, plus the auto-generated
 * lambda execution roles) are each granted only the specific DynamoDB table +
 * index ARNs, S3 bucket ARN + prefix, and Bedrock model / AgentCore ARNs they
 * use. There are NO wildcard resource entries.
 */
export class VillageStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: VillageStackProps) {
    super(scope, id, props);

    const { environment, simulationId } = props;
    const isDev = environment === 'dev';
    const region = cdk.Stack.of(this).region;
    const account = cdk.Stack.of(this).account;

    // Image-generation region for the Asset Generator (DESIGN.md §1). This is a
    // runtime parameter only — no resource is created there.
    const IMAGE_REGION = 'us-west-2';
    const IMAGE_MODEL_ID = 'stability.stable-image-ultra-v1:1';

    // Reasoning + fast models used by the engine/harness via cross-region
    // inference profiles (DESIGN.md §1).
    const OPUS_PROFILE = 'au.anthropic.claude-opus-5';
    const HAIKU_PROFILE = 'au.anthropic.claude-haiku-4-5-20251001-v1:0';
    // Underlying foundation-model id fragments for the two models. Cross-region
    // inference invokes the profile AND the member-region foundation models, so
    // both sets of ARNs are enumerated explicitly (no wildcards — Req 17.6).
    const OPUS_FM = 'anthropic.claude-opus-5';
    const HAIKU_FM = 'anthropic.claude-haiku-4-5-20251001-v1:0';
    // Member regions of the `au.` inference profile family + us-west-2 (used by
    // some cross-region routes). Enumerated so foundation-model ARNs stay explicit.
    const INFERENCE_MEMBER_REGIONS = ['ap-southeast-2', 'ap-southeast-4', 'us-west-2', 'us-east-1'];

    // -------------------------------------------------------------------------
    // 1. DynamoDB single-table (DESIGN.md §3, Req 13 / 17.2)
    // -------------------------------------------------------------------------
    const table = new dynamodb.Table(this, 'WorldStateTable', {
      tableName: `village-${simulationId}-${environment}`,
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: !isDev,
      removalPolicy: isDev ? cdk.RemovalPolicy.DESTROY : cdk.RemovalPolicy.RETAIN,
    });

    // GSI1: event-log queries by category + time (DESIGN.md §3).
    table.addGlobalSecondaryIndex({
      indexName: 'GSI1',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    // GSI2: event-log queries by agent + time (DESIGN.md §3).
    table.addGlobalSecondaryIndex({
      indexName: 'GSI2',
      partitionKey: { name: 'GSI2PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI2SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Explicit table + index ARNs used everywhere below (no wildcard resources).
    const tableArn = table.tableArn;
    const indexArns = [`${tableArn}/index/GSI1`, `${tableArn}/index/GSI2`];

    // Reusable statement factory: DynamoDB read/write on table + its two indexes.
    const dynamoRwStatement = () =>
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:BatchGetItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:BatchWriteItem',
          'dynamodb:ConditionCheckItem',
        ],
        resources: [tableArn, ...indexArns],
      });

    // -------------------------------------------------------------------------
    // 2. Assets S3 bucket (Req 16 / 17.2)
    // -------------------------------------------------------------------------
    const assetsBucket = new s3.Bucket(this, 'AssetsBucket', {
      bucketName: `village-assets-${simulationId}-${environment}-${account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: false,
      removalPolicy: isDev ? cdk.RemovalPolicy.DESTROY : cdk.RemovalPolicy.RETAIN,
      autoDeleteObjects: isDev,
      // The SPA fetches asset images via /v1/sim/{id}/assets/{id}, which returns
      // a 302 to a presigned S3 URL. useAuthImage follows that redirect with
      // fetch(), so the browser performs a cross-origin GET against this bucket
      // and needs CORS headers to READ the response. Presigned URLs are already
      // access-controlled by their signature, so allowing GET/HEAD from any
      // origin is safe. Without this, portraits silently fall back to placeholders.
      cors: [
        {
          allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.HEAD],
          allowedOrigins: ['*'],
          allowedHeaders: ['*'],
          exposedHeaders: ['ETag'],
          maxAge: 3000,
        },
      ],
    });

    // -------------------------------------------------------------------------
    // 4. Asset Generator Lambda (Req 16). Defined before the API lambda because
    //    the API lambda is granted lambda:InvokeFunction on this function's ARN.
    // -------------------------------------------------------------------------
    const assetFn = new lambda.Function(this, 'AssetGeneratorFunction', {
      functionName: `village-assets-${simulationId}-${environment}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'assets')),
      timeout: cdk.Duration.minutes(15),
      memorySize: 1024,
      environment: {
        TABLE_NAME: table.tableName,
        ASSETS_BUCKET: assetsBucket.bucketName,
        IMAGE_REGION,
        MODEL_ID: IMAGE_MODEL_ID,
        TABLE_REGION: region,
      },
    });

    // DynamoDB RW on table + indexes.
    assetFn.addToRolePolicy(dynamoRwStatement());
    // S3 read/write on the assets bucket (bucket ARN + objects prefix).
    assetsBucket.grantReadWrite(assetFn);
    // Bedrock: only the specific Stable Diffusion model ARN in us-west-2.
    assetFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [`arn:aws:bedrock:${IMAGE_REGION}::foundation-model/${IMAGE_MODEL_ID}`],
      })
    );

    // -------------------------------------------------------------------------
    // 3. Simulation API Lambda (Req 5 §5, Req 17.4/5)
    // -------------------------------------------------------------------------
    const apiFn = new lambda.Function(this, 'SimulationApiFunction', {
      functionName: `village-api-${simulationId}-${environment}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'api')),
      timeout: cdk.Duration.seconds(29),
      memorySize: 512,
      environment: {
        TABLE_NAME: table.tableName,
        ASSETS_BUCKET: assetsBucket.bucketName,
        ASSET_FN_NAME: assetFn.functionName,
        // AWS_REGION is a reserved Lambda env var (set automatically); the code
        // reads it directly, so we do not set it here.
        ALLOW_ANON: isDev ? 'false' : 'false',
      },
    });

    // DynamoDB RW on table + indexes only.
    apiFn.addToRolePolicy(dynamoRwStatement());
    // S3 read + explicit GetObject/PutObject on the bucket ARN + '/*'.
    assetsBucket.grantRead(apiFn);
    apiFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['s3:GetObject', 's3:PutObject'],
        resources: [`${assetsBucket.bucketArn}/*`],
      })
    );
    // lambda:InvokeFunction on the asset lambda ARN only.
    apiFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['lambda:InvokeFunction'],
        resources: [assetFn.functionArn],
      })
    );

    // -------------------------------------------------------------------------
    // 5. Cognito User Pool + Client, HTTP API with JWT authorizer (Req 17.4/5)
    // -------------------------------------------------------------------------
    const userPool = new cognito.UserPool(this, 'OperatorUserPool', {
      userPoolName: `village-${simulationId}-${environment}`,
      selfSignUpEnabled: false,
      signInAliases: { email: true, username: true },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: isDev ? cdk.RemovalPolicy.DESTROY : cdk.RemovalPolicy.RETAIN,
    });

    const userPoolClient = userPool.addClient('OperatorUserPoolClient', {
      userPoolClientName: `village-spa-${environment}`,
      generateSecret: false,
      authFlows: {
        userPassword: true,
        userSrp: true,
      },
      idTokenValidity: cdk.Duration.hours(1),
      accessTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
    });

    const httpApi = new apigwv2.HttpApi(this, 'SimulationHttpApi', {
      apiName: `village-api-${simulationId}-${environment}`,
      corsPreflight: {
        allowOrigins: ['*'],
        allowMethods: [
          apigwv2.CorsHttpMethod.GET,
          apigwv2.CorsHttpMethod.POST,
          apigwv2.CorsHttpMethod.OPTIONS,
        ],
        allowHeaders: ['Authorization', 'Content-Type'],
      },
    });

    const jwtAuthorizer = new apigwv2Authorizers.HttpUserPoolAuthorizer(
      'OperatorJwtAuthorizer',
      userPool,
      {
        userPoolClients: [userPoolClient],
      }
    );

    const apiIntegration = new apigwv2Integrations.HttpLambdaIntegration(
      'SimulationApiIntegration',
      apiFn
    );

    // Register the authorized route ONLY for the real methods the API serves
    // (GET/POST). We deliberately do NOT use HttpMethod.ANY here: ANY also
    // matches the CORS preflight `OPTIONS` request, and attaching the JWT
    // authorizer to it causes API Gateway to reject the preflight with 401
    // (browsers never send an Authorization header on preflight). That 401
    // surfaces in the browser as a CORS error. By restricting the authorized
    // route to GET/POST, the HttpApi's built-in `corsPreflight` handling
    // answers OPTIONS unauthenticated with the correct CORS headers.
    httpApi.addRoutes({
      path: '/v1/{proxy+}',
      methods: [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
      integration: apiIntegration,
      authorizer: jwtAuthorizer,
    });

    // -------------------------------------------------------------------------
    // 6/7. ECR image assets: Engine (amd64) + Harness (arm64)
    // -------------------------------------------------------------------------
    const engineImage = new ecrAssets.DockerImageAsset(this, 'EngineImage', {
      directory: path.join(__dirname, '..', '..', 'engine'),
      platform: ecrAssets.Platform.LINUX_ARM64,
    });

    // Harness image pushed to ECR so the orchestrator can create the AgentCore
    // Runtime from it out-of-band. ARM64 per AgentCore requirement (DESIGN.md §2).
    const harnessImage = new ecrAssets.DockerImageAsset(this, 'HarnessImage', {
      directory: path.join(__dirname, '..', '..', 'harness'),
      platform: ecrAssets.Platform.LINUX_ARM64,
    });

    // -------------------------------------------------------------------------
    // AgentCore Runtime + Memory ARNs/IDs are created out-of-band by the
    // orchestrator AFTER the harness image is in ECR. They are supplied here as
    // CfnParameters that the Fargate task reads. Defaults are account-scoped ARN
    // patterns so the stack synthesises/deploys before the runtime exists; the
    // orchestrator updates the stack (or SSM) once the runtime/memory are made.
    // -------------------------------------------------------------------------
    const agentRuntimeArnParam = new cdk.CfnParameter(this, 'AgentRuntimeArn', {
      type: 'String',
      description:
        'ARN of the Bedrock AgentCore Runtime (created out-of-band from the harness image).',
      default: `arn:aws:bedrock-agentcore:${region}:${account}:runtime/village-${simulationId}-${environment}`,
    });
    const memoryIdParam = new cdk.CfnParameter(this, 'MemoryId', {
      type: 'String',
      description: 'ID of the Bedrock AgentCore Memory resource (created out-of-band).',
      default: `village-${simulationId}-${environment}`,
    });
    const memoryArnParam = new cdk.CfnParameter(this, 'MemoryArn', {
      type: 'String',
      description: 'ARN of the Bedrock AgentCore Memory resource (created out-of-band).',
      default: `arn:aws:bedrock-agentcore:${region}:${account}:memory/village-${simulationId}-${environment}`,
    });

    // -------------------------------------------------------------------------
    // 6. ECS Fargate Simulation Engine
    // -------------------------------------------------------------------------
    // Reuse the account's existing DEFAULT VPC (public subnets) rather than
    // creating a new one: this region is at its VPC / Internet Gateway limit.
    // Fargate tasks run in the default public subnets with a public IP so they
    // can reach AWS service endpoints without a NAT gateway.
    const vpc = ec2.Vpc.fromLookup(this, 'EngineVpc', {
      isDefault: true,
    });

    const cluster = new ecs.Cluster(this, 'EngineCluster', {
      vpc,
      clusterName: `village-${simulationId}-${environment}`,
      containerInsights: true,
    });

    const engineTaskRole = new iam.Role(this, 'EngineTaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'Task role for the Simulation Engine Fargate task (least-privilege).',
    });

    // DynamoDB RW on table + indexes.
    engineTaskRole.addToPolicy(dynamoRwStatement());

    // Bedrock: explicit inference-profile ARNs for the two models (no wildcards).
    const inferenceProfileArns = [
      `arn:aws:bedrock:${region}:${account}:inference-profile/${OPUS_PROFILE}`,
      `arn:aws:bedrock:${region}:${account}:inference-profile/${HAIKU_PROFILE}`,
    ];
    // Underlying foundation-model ARNs in every member region of the profiles.
    const foundationModelArns: string[] = [];
    for (const r of INFERENCE_MEMBER_REGIONS) {
      foundationModelArns.push(`arn:aws:bedrock:${r}::foundation-model/${OPUS_FM}`);
      foundationModelArns.push(`arn:aws:bedrock:${r}::foundation-model/${HAIKU_FM}`);
    }
    engineTaskRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [...inferenceProfileArns, ...foundationModelArns],
      })
    );

    // AgentCore Runtime invocation + Memory operations scoped to the param ARNs.
    engineTaskRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [agentRuntimeArnParam.valueAsString, `${agentRuntimeArnParam.valueAsString}/*`],
      })
    );
    engineTaskRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock-agentcore:CreateEvent',
          'bedrock-agentcore:RetrieveMemoryRecords',
          'bedrock-agentcore:ListEvents',
        ],
        resources: [memoryArnParam.valueAsString, `${memoryArnParam.valueAsString}/*`],
      })
    );

    const engineLogGroup = new logs.LogGroup(this, 'EngineLogGroup', {
      logGroupName: `/village/${simulationId}/${environment}/engine`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: isDev ? cdk.RemovalPolicy.DESTROY : cdk.RemovalPolicy.RETAIN,
    });

    const engineTaskDef = new ecs.FargateTaskDefinition(this, 'EngineTaskDef', {
      // Vertically scaled from 0.5 vCPU/1GB to 1 vCPU/2GB. The engine fans out
      // per-agent harness calls with bounded concurrency (<=8 threads) each
      // tick; the extra CPU/memory gives headroom for 25 agents' concurrent
      // network I/O, JSON (de)serialisation, and route computation without the
      // tick loop falling behind real time. (Valid ARM64 Fargate pair.)
      cpu: 1024,
      memoryLimitMiB: 2048,
      taskRole: engineTaskRole,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    engineTaskDef.addContainer('EngineContainer', {
      containerName: 'engine',
      image: ecs.ContainerImage.fromDockerImageAsset(engineImage),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'engine',
        logGroup: engineLogGroup,
      }),
      environment: {
        TABLE_NAME: table.tableName,
        SIM_ID: simulationId,
        AWS_REGION: region,
        AGENT_RUNTIME_ARN: agentRuntimeArnParam.valueAsString,
        MEMORY_ID: memoryIdParam.valueAsString,
        LOOP_INTERVAL_SEC: '1',
      },
    });

    const engineService = new ecs.FargateService(this, 'EngineService', {
      cluster,
      serviceName: `village-engine-${simulationId}-${environment}`,
      taskDefinition: engineTaskDef,
      desiredCount: 1,
      assignPublicIp: true,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
    });

    // -------------------------------------------------------------------------
    // 8. Static SPA hosting: private S3 bucket + CloudFront (OAC)
    // -------------------------------------------------------------------------
    const spaBucket = new s3.Bucket(this, 'SpaBucket', {
      bucketName: `village-spa-${simulationId}-${environment}-${account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: isDev ? cdk.RemovalPolicy.DESTROY : cdk.RemovalPolicy.RETAIN,
      autoDeleteObjects: isDev,
    });

    const distribution = new cloudfront.Distribution(this, 'SpaDistribution', {
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(spaBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      defaultRootObject: 'index.html',
      // SPA client-side routing: serve index.html for 403/404.
      errorResponses: [
        {
          httpStatus: 403,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.minutes(5),
        },
        {
          httpStatus: 404,
          responseHttpStatus: 200,
          responsePagePath: '/index.html',
          ttl: cdk.Duration.minutes(5),
        },
      ],
      comment: `Melbourne Agent Village SPA (${environment})`,
    });

    // Deploy the built SPA if dist exists; otherwise deploy a placeholder so the
    // bucket + distribution are valid and the orchestrator can upload dist later.
    const distDir = path.join(__dirname, '..', '..', 'web', 'dist');
    const distExists =
      fs.existsSync(distDir) && fs.existsSync(path.join(distDir, 'index.html'));
    const spaSource = distExists
      ? s3deploy.Source.asset(distDir)
      : s3deploy.Source.data(
          'index.html',
          '<!doctype html><html><head><meta charset="utf-8"><title>Melbourne Agent Village</title></head>' +
            '<body><p>SPA build pending — the orchestrator will upload web/dist.</p></body></html>'
        );

    new s3deploy.BucketDeployment(this, 'SpaDeployment', {
      sources: [spaSource],
      destinationBucket: spaBucket,
      distribution,
      distributionPaths: ['/*'],
      prune: true,
    });

    // -------------------------------------------------------------------------
    // 9. CfnOutputs (stable logical names)
    // -------------------------------------------------------------------------
    new cdk.CfnOutput(this, 'TableName', { value: table.tableName });
    new cdk.CfnOutput(this, 'AssetsBucketName', { value: assetsBucket.bucketName });
    new cdk.CfnOutput(this, 'ApiEndpoint', { value: httpApi.apiEndpoint });
    new cdk.CfnOutput(this, 'UserPoolId', { value: userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', { value: userPoolClient.userPoolClientId });
    new cdk.CfnOutput(this, 'EngineClusterName', { value: cluster.clusterName });
    new cdk.CfnOutput(this, 'EngineServiceName', { value: engineService.serviceName });
    new cdk.CfnOutput(this, 'HarnessImageUri', { value: harnessImage.imageUri });
    new cdk.CfnOutput(this, 'SpaBucketName', { value: spaBucket.bucketName });
    new cdk.CfnOutput(this, 'SpaUrl', { value: `https://${distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, 'AssetFunctionName', { value: assetFn.functionName });
  }
}
