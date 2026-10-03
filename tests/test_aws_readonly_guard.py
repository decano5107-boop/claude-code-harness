"""
Tests for hooks/aws_readonly_guard.py, organised by threat:

  1. A write reaches AWS through some spelling the guard does not recognise.
  2. A write hides behind a read on the same Bash line.
  3. A secret is read without a human seeing the request.
  4. The guard gets in the way of ordinary work (reads, non-AWS commands).
  5. The guard auto-approves something ("allow" must never be emitted).
"""
from __future__ import annotations

import unittest

from helpers import HookTestCase

HOOK = "aws_readonly_guard.py"
MCP_TOOL = "mcp__aws_api__call_aws"


class GuardCase(HookTestCase):

    def bash(self, command: str) -> dict | None:
        code, out, err = self.run_hook(HOOK, {"tool_name": "Bash",
                                              "tool_input": {"command": command}})
        self.assertEqual(code, 0, err)
        self.assertNotIn("failed open", err)  # silence must be a verdict, not a crash
        return out

    def mcp(self, cli_command) -> dict | None:
        code, out, err = self.run_hook(HOOK, {"tool_name": MCP_TOOL,
                                              "tool_input": {"cli_command": cli_command}})
        self.assertEqual(code, 0, err)
        self.assertNotIn("failed open", err)  # silence must be a verdict, not a crash
        return out

    def decision(self, out: dict | None) -> str | None:
        if out is None:
            return None
        return out["hookSpecificOutput"]["permissionDecision"]

    def assertDenied(self, command: str) -> None:
        self.assertEqual(self.decision(self.bash(command)), "deny", command)

    def assertAsks(self, command: str) -> None:
        self.assertEqual(self.decision(self.bash(command)), "ask", command)

    def assertSilent(self, command: str) -> None:
        self.assertIsNone(self.bash(command), command)


class WritesAreDenied(GuardCase):
    """Threat 1: every way of spelling a mutating call ends in deny."""

    def test_plain_mutations(self):
        for cmd in ("aws ec2 terminate-instances --instance-ids i-0abc",
                    "aws iam create-user --user-name x",
                    "aws dynamodb put-item --table-name t --item {}",
                    "aws lambda update-function-code --function-name f",
                    "aws athena start-query-execution --query-string 'DROP TABLE t'",
                    "aws cloudformation deploy --template-file t.yaml",
                    "aws some-future-service frobnicate-everything"):
            self.assertDenied(cmd)

    def test_s3_high_level_writes(self):
        for cmd in ("aws s3 rm s3://bucket/key",
                    "aws s3 rm s3://bucket --recursive",
                    "aws s3 mv s3://bucket/a ./a",
                    "aws s3 mb s3://new-bucket",
                    "aws s3 rb s3://bucket --force",
                    "aws s3 cp ./file s3://bucket/file",
                    "aws s3 cp - s3://bucket/file",
                    "aws s3 cp s3://a/x s3://b/x",
                    "aws s3 sync ./dir s3://bucket/dir",
                    "aws s3 website s3://bucket --index-document index.html"):
            self.assertDenied(cmd)

    def test_wrappers_and_paths(self):
        for cmd in ("/usr/local/bin/aws s3 rm s3://b/k",
                    "sudo aws s3 rm s3://b/k",
                    "sudo -u ops aws s3 rm s3://b/k",
                    "env AWS_PROFILE=x aws s3 rm s3://b/k",
                    "AWS_PROFILE=x aws s3 rm s3://b/k",
                    "timeout 30 aws s3 rm s3://b/k",
                    "nohup aws s3 rm s3://b/k &",
                    "command aws s3 rm s3://b/k",
                    "echo s3://b/k | xargs -I{} aws s3 rm {}",
                    "find . -name '*.tmp' -exec aws s3 rm {} ';'",
                    "aws-vault exec prod -- aws s3 rm s3://b/k",
                    "aws-vault exec prod aws s3 rm s3://b/k",
                    "AWS s3 rm s3://b/k",
                    "watch -n 5 'aws s3 rm s3://b/k'",
                    "env -S 'aws s3 rm s3://b/k'"):
            self.assertDenied(cmd)

    def test_global_flags_do_not_hide_the_operation(self):
        for cmd in ("aws --profile p s3 rm s3://b/k",
                    "aws --region us-east-1 --output json ec2 terminate-instances --instance-ids i-1",
                    "aws --profile=p --debug s3 rm s3://b/k",
                    "aws --endpoint-url http://localhost:4566 s3 rm s3://b/k",
                    "aws --query Foo --profile describe-instances iam create-user --user-name x"):
            self.assertDenied(cmd)

    def test_quoting_does_not_hide_the_operation(self):
        for cmd in ("aws s3 'rm' s3://b/k", 'aws s3 "rm" s3://b/k', "aws s3 r\\m s3://b/k",
                    "a''ws s3 rm s3://b/k", "a\\ws s3 rm s3://b/k"):
            self.assertDenied(cmd)

    def test_nested_shells_and_substitutions(self):
        for cmd in ('bash -c "aws s3 rm s3://b/k"',
                    "sh -lc 'aws iam delete-user --user-name x'",
                    "eval aws s3 rm s3://b/k",
                    "echo $(aws s3 rm s3://b/k)",
                    'echo "$(aws s3 rm s3://b/k)"',
                    "echo `aws s3 rm s3://b/k`",
                    "(cd /tmp && aws s3 rm s3://b/k)",
                    "diff <(aws s3 ls) <(aws s3 rm s3://b/k)"):
            self.assertDenied(cmd)

    def test_deny_reason_names_the_operation_and_carries_the_hint(self):
        out = self.bash("aws s3 rm s3://b/k")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("s3 rm", reason)
        self.assertIn("Ask the user to run it themselves", reason)


class ChainedCommands(GuardCase):
    """Threat 2: the worst segment decides; a read cannot launder a write."""

    def test_write_after_read(self):
        for cmd in ("aws s3 ls && aws s3 rm s3://b/k",
                    "aws s3 ls; aws s3 rm s3://b/k",
                    "aws s3 ls || aws s3 rm s3://b/k",
                    "aws s3 ls | aws s3 rm s3://b/k",
                    "aws s3 ls\naws s3 rm s3://b/k",
                    "aws s3 ls # harmless\naws s3 rm s3://b/k",
                    "aws s3 ls 2>&1 >/dev/null; aws s3 rm s3://b/k"):
            self.assertDenied(cmd)

    def test_deny_beats_ask(self):
        self.assertDenied("aws secretsmanager get-secret-value --secret-id s && aws s3 rm s3://b/k")
        self.assertDenied("aws s3 rm s3://b/k; aws ssm get-parameter --name n")

    def test_ask_beats_read(self):
        self.assertAsks("aws s3 ls && aws secretsmanager get-secret-value --secret-id s")

    def test_operators_inside_quotes_are_not_separators(self):
        self.assertSilent('aws logs filter-log-events --log-group-name g --filter-pattern "rm | delete; x"')


class ShellGrammarAndRunners(GuardCase):
    """Threat 1, continued: shell keywords and program runners do not hide a write."""

    def test_shell_keywords_do_not_hide_the_command(self):
        for cmd in ("if aws s3 rb s3://b --force; then :; fi",
                    "if true; then aws s3 rb s3://b --force; fi",
                    "for b in x; do aws s3 rb s3://$b --force; done",
                    "while true; do aws s3 rm s3://b/k; done",
                    "{ aws s3 rb s3://b --force; }",
                    "! aws s3 rb s3://b --force"):
            self.assertDenied(cmd)

    def test_unknown_runner_carrying_aws_fails_closed(self):
        for cmd in ("flock /tmp/l aws s3 rb s3://b --force",
                    "uv run aws s3 rb s3://b --force",
                    "parallel aws s3 rm ::: s3://b/k"):
            self.assertDenied(cmd)

    def test_xargs_supplying_the_operation_is_denied(self):
        self.assertDenied("printf 's3\\nrb\\ns3://b\\n' | xargs aws")

    def test_python_module_awscli_is_the_cli(self):
        self.assertDenied("python3 -m awscli s3 rb s3://b --force")
        self.assertSilent("python3 -m awscli s3 ls")

    def test_shell_reading_stdin_is_denied(self):
        self.assertDenied("echo 'aws s3 rb s3://b --force' | bash")

    def test_ecr_public_login_asks(self):
        self.assertAsks("aws ecr-public get-login-password --region us-east-1")

    def test_keywords_and_data_commands_stay_quiet(self):
        for cmd in ("for f in *.tf; do terraform fmt $f; done",
                    "if [ -f x ]; then echo aws; fi",
                    "grep -rn aws .", "pip install awscli", "brew upgrade awscli"):
            self.assertSilent(cmd)


class SecretsAsk(GuardCase):
    """Threat 3: secret material always goes past a human."""

    def test_secret_reads(self):
        for cmd in ("aws secretsmanager get-secret-value --secret-id prod/db",
                    "aws secretsmanager batch-get-secret-value --secret-id-list a b",
                    "aws ssm get-parameter --name /app/key",
                    "aws ssm get-parameters --names a b",
                    "aws ssm get-parameters-by-path --path /app",
                    "aws ssm get-parameter-history --name /app/key --with-decryption",
                    "aws kms decrypt --ciphertext-blob fileb://x",
                    "aws sts get-session-token",
                    "aws ecr get-login-password",
                    "aws configure get aws_secret_access_key",
                    "aws configure export-credentials"):
            self.assertAsks(cmd)

    def test_credentials_behind_a_get_verb(self):
        # `get-` would pass them as reads; each returns a usable credential.
        for cmd in ("aws sso get-role-credentials --role-name r --account-id 111122223333 --access-token t",
                    "aws cognito-identity get-credentials-for-identity --identity-id id",
                    "aws lightsail get-instance-access-details --instance-name web",
                    "aws ec2 get-password-data --instance-id i-0abc --priv-launch-key k.pem",
                    "aws --region eu-west-1 ec2 get-password-data --instance-id i-0abc"):
            self.assertAsks(cmd)

    def test_credential_calls_ask_through_mcp(self):
        out = self.mcp("aws sso get-role-credentials --role-name r --account-id 1 --access-token t")
        self.assertEqual(self.decision(out), "ask")

    def test_neighbouring_reads_stay_silent(self):
        # Only the listed operations ask; the rest of each service stays a read.
        for cmd in ("aws ec2 get-console-output --instance-id i-0abc",
                    "aws lightsail get-instances",
                    "aws cognito-identity get-id --identity-pool-id p",
                    "aws sso list-accounts --access-token t",
                    "aws s3api get-object-tagging --bucket b --key k"):
            self.assertSilent(cmd)

    def test_downloads_ask(self):
        for cmd in ("aws s3 cp s3://bucket/report.csv ./report.csv",
                    "aws s3 cp s3://bucket/report.csv -",
                    "aws s3 cp --recursive s3://bucket/dir ./dir",
                    "aws s3 sync s3://bucket/dir ./dir",
                    "aws s3api get-object --bucket b --key k out.bin",
                    "aws s3api get-object-torrent --bucket b --key k out.torrent",
                    "aws s3api select-object-content --bucket b --key k "
                    "--expression 'SELECT * FROM S3Object' --expression-type SQL "
                    "--input-serialization '{}' --output-serialization '{}' out.csv",
                    "aws s3 presign s3://bucket/key"):
            self.assertAsks(cmd)


class ReadsStaySilent(GuardCase):
    """Threats 4 and 5: reads are not blocked, and nothing is ever auto-approved."""

    def test_reads(self):
        for cmd in ("aws s3 ls",
                    "aws s3 ls s3://bucket/prefix/ --recursive",
                    "aws sts get-caller-identity",
                    "aws --profile p --region eu-west-1 ec2 describe-instances",
                    "aws ec2 --region eu-west-1 describe-instances",
                    "aws iam list-roles --output table",
                    "aws dynamodb scan --table-name t",
                    "aws dynamodb query --table-name t --key-condition-expression x",
                    "aws dynamodb batch-get-item --request-items file://r.json",
                    "aws s3api head-object --bucket b --key k",
                    "aws logs filter-log-events --log-group-name g",
                    "aws logs start-query --log-group-name g --query-string q",
                    "aws logs get-query-results --query-id q",
                    "aws logs tail /aws/lambda/f --follow",
                    "aws iam simulate-principal-policy --policy-source-arn arn:aws:iam::111122223333:role/r",
                    "aws ce get-cost-and-usage --time-period Start=a,End=b",
                    "aws configure get region",
                    "aws configure list-profiles",
                    "aws help", "aws s3 help", "aws ec2 terminate-instances help",
                    "aws --version", "aws",
                    "aws bedrock-runtime invoke-model --model-id m --body '{}' out.json",
                    "aws bedrock-runtime converse --model-id m --messages '[]'"):
            self.assertSilent(cmd)

    def test_chained_reads_with_non_aws_commands(self):
        # Silent, not "allow": the `rm` must still go through normal permissions.
        self.assertSilent("aws s3 ls > out.txt && rm -rf build && aws sts get-caller-identity")

    def test_non_aws_commands(self):
        for cmd in ("ls -la", "grep -r aws src/", "cat ~/.aws/config",
                    "git commit -m 'fix aws s3 rm handling'",
                    "brew install awscli", "which aws", "echo \"deploy to aws\"",
                    "pip install aws-cdk-lib"):
            self.assertSilent(cmd)

    def test_other_tools_ignored(self):
        code, out, _ = self.run_hook(HOOK, {"tool_name": "Write",
                                            "tool_input": {"file_path": "x",
                                                           "content": "aws s3 rm s3://b/k"}})
        self.assertEqual((code, out), (0, None))

    def test_never_emits_allow(self):
        for cmd in ("aws s3 ls", "aws s3 rm s3://b/k", "aws ssm get-parameter --name n"):
            out = self.bash(cmd)
            if out is not None:
                self.assertNotEqual(self.decision(out), "allow")


class FailsClosed(GuardCase):
    """Unparseable AWS text is denied; unparseable non-AWS text is left alone."""

    def test_unbalanced_quote_with_aws(self):
        self.assertDenied("aws s3 ls 'unterminated")

    def test_unbalanced_quote_without_aws(self):
        self.assertSilent("echo 'unterminated")

    def test_service_without_operation(self):
        self.assertDenied("aws s3")

    def test_unknown_global_option(self):
        # Whether `--mystery` takes a value decides what the service is; do not guess.
        self.assertDenied("aws --mystery s3 ls")

    def test_empty_and_garbage_events(self):
        for event in ("", "not json", "[]", '{"tool_name": "Bash"}',
                      '{"tool_name": "Bash", "tool_input": "aws s3 rm"}'):
            code, out, _ = self.run_hook(HOOK, event)
            self.assertEqual((code, out), (0, None), event)


class McpTool(GuardCase):
    """The AWS MCP server's call_aws tool gets the same rules, and fails closed."""

    def test_read_silent(self):
        self.assertIsNone(self.mcp("aws s3 ls"))

    def test_write_denied(self):
        self.assertEqual(self.decision(self.mcp("aws s3 rm s3://b/k")), "deny")
        self.assertEqual(self.decision(self.mcp("aws --profile p iam create-user --user-name x")), "deny")

    def test_secret_asks(self):
        self.assertEqual(self.decision(self.mcp("aws secretsmanager get-secret-value --secret-id s")), "ask")

    def test_unreadable_input_denied(self):
        for bad in ("", "s3 rm s3://b/k", "aws s3 ls 'oops", None, 42):
            self.assertEqual(self.decision(self.mcp(bad)), "deny", repr(bad))


class Configuration(GuardCase):

    def test_allow_extra(self):
        self.write_config({"aws_guard": {"allow_extra": ["cloudformation validate-template"]}})
        self.assertSilent("aws cloudformation validate-template --template-body file://t.yaml")
        self.assertDenied("aws cloudformation deploy --template-file t.yaml")

    def test_ask_extra_wins_over_reads_and_allow_extra(self):
        self.write_config({"aws_guard": {"ask_extra": ["dynamodb scan"],
                                         "allow_extra": ["dynamodb scan"]}})
        self.assertAsks("aws dynamodb scan --table-name t")

    def test_model_invocation_switch(self):
        self.write_config({"aws_guard": {"allow_model_invocation": False}})
        self.assertDenied("aws bedrock-runtime invoke-model --model-id m --body '{}' out.json")

    def test_deny_message_hint(self):
        self.write_config({"aws_guard": {"deny_message_hint": "File a change request."}})
        out = self.bash("aws s3 rm s3://b/k")
        self.assertTrue(out["hookSpecificOutput"]["permissionDecisionReason"]
                        .endswith("File a change request."))

    def test_malformed_config_values_fall_back_to_defaults(self):
        self.write_config({"aws_guard": {"allow_extra": "s3 rm", "allow_model_invocation": "no"}})
        self.assertDenied("aws s3 rm s3://b/k")
        self.assertSilent("aws bedrock-runtime converse --model-id m --messages '[]'")

    def test_decisions_are_logged(self):
        import json
        import os
        self.bash("aws s3 rm s3://b/k")
        with open(os.path.join(self.state, "aws_guard.jsonl"), encoding="utf-8") as f:
            record = json.loads(f.readline())
        self.assertEqual(record["decision"], "deny")


if __name__ == "__main__":
    unittest.main()
