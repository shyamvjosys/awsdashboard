"""
Central account config for AWS cost/report scripts.
"""

LINKED_ACCOUNTS = {
    "josys-master": "055184381049",
    "josys-dev2": "137575128411",
    "pre-staging": "340752826805",
    "idac-qa": "905418452628",
    "josys-prod": "351343535468",
    "idac-prod": "637423183342",
    "josys-staging": "598628178244",
    "caf-rpa": "152426705810",
    "josys-us": "229468566969",
    "jep-devqa": "147997136868",
    "jep-prod": "688567279148",
    "jep-stage": "463470957463",
}

PAYER_PROFILE = "josys-master"
PAYER_ACCOUNT = "055184381049"

PROFILES = {
    "josys-master": {"profile": "josys-master", "account": "055184381049"},
    "josys-dev2": {"profile": "josys-dev2", "account": "137575128411"},
    "pre-staging": {"profile": "josys-prestaging", "account": "340752826805"},
    "idac-qa": {"profile": "idac-qa", "account": "905418452628"},
    "josys-prod": {"profile": "josys-prod", "account": "351343535468"},
    "idac-prod": {"profile": "idac-prod", "account": "637423183342"},
    "josys-staging": {"profile": "josys-staging", "account": "598628178244"},
    "caf-rpa": {"profile": "caf-rpa", "account": "152426705810"},
    "josys-us": {"profile": "josys-us", "account": "229468566969"},
    "jep-devqa": {"profile": "jep-devqa", "account": "147997136868"},
    "jep-prod": {"profile": "jep-prod", "account": "688567279148"},
    "jep-stage": {"profile": "jep-stage", "account": "463470957463"},
}

REGION = "ap-northeast-1"
REGIONS = ["ap-northeast-1", "us-east-2"]
REGION_LABELS = {
    "ap-northeast-1": "Tokyo",
    "us-east-2": "US East (Ohio)",
}


def configured_aws_profiles() -> set[str]:
    try:
        import boto3
    except ImportError:
        return set()
    return set(boto3.Session().available_profiles)


def resolve_profiles_for_run(target: str = "all"):
    if target != "all" and target not in PROFILES:
        raise KeyError(target)

    wanted = list(PROFILES.items()) if target == "all" else [(target, PROFILES[target])]
    available = configured_aws_profiles()
    runnable = []
    skipped = []
    for env_name, cfg in wanted:
        aws_profile = cfg["profile"]
        if aws_profile in available:
            runnable.append((env_name, cfg))
        else:
            skipped.append(
                (
                    env_name,
                    f"AWS profile '{aws_profile}' not in ~/.aws/config "
                    f"(account {cfg['account']})",
                )
            )
    return runnable, skipped
