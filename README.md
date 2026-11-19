# sample-serverless-batch-orchestration-for-mainframe-modernization

[![Status](https://img.shields.io/badge/Status-Incubating-blue)]()
[![Huawei Cloud](https://img.shields.io/badge/Huawei%20Cloud-Samples-red)]()
[![Scenario](https://img.shields.io/badge/Scenario-finance-success)](https://3ms.huawei.com/docs/docinfo/1300901382590492672?bookstackId=866760814559879168&gid=3591759&l=zh-cn&documentkind=&attachmentIdx=5)

End-of-day batch still runs as job chains in many estates: one job normalises a landed extract, two jobs consume its output in parallel, a final job merges the two branches into the settlement. Modernising that workload is not hard because of any single job — it is hard because of the **orchestration**: which job waits for which, whether today is a processing day, whether the input file of a step has arrived yet, how a chain converges after one step fails, and how an execution that lasts hours survives a function invocation that lasts minutes

## Overview

This repository is created from the huaweicloud-samples automated repository request workflow.

## Getting Started

Add setup, deployment, and verification steps here.

## Contributing

Please use pull requests and follow the repository review rules.

## License

This project is licensed under the MIT-0 license.

## Maintainers

CODEOWNERS: @Ferguson2211

## Feedback

Please use GitHub Issues: https://github.com/huaweicloud-samples/sample-serverless-batch-orchestration-for-mainframe-modernization/issues
