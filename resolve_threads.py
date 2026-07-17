import subprocess

threads = [
    "PRRT_kwDOTEbyfc6R1XuH",
    "PRRT_kwDOTEbyfc6R1XuP",
    "PRRT_kwDOTEbyfc6R1cbh",
    "PRRT_kwDOTEbyfc6R1cb5",
    "PRRT_kwDOTEbyfc6R1ccJ",
    "PRRT_kwDOTEbyfc6R1lmy",
    "PRRT_kwDOTEbyfc6R1pTv",
    "PRRT_kwDOTEbyfc6R1pT5",
    "PRRT_kwDOTEbyfc6R1pUI",
]

reply_mutation = """
mutation AddPullRequestReviewThreadReply($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestThreadId: $threadId, body: $body}) {
    comment {
      id
    }
  }
}
"""

resolve_mutation = """
mutation ResolveThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread {
      isResolved
    }
  }
}
"""

for tid in threads:
    print(f"Resolving {tid}...")
    subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=" + reply_mutation,
            "-f",
            f"threadId={tid}",
            "-f",
            "body=Fixed.",
        ]
    )
    subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=" + resolve_mutation,
            "-f",
            f"threadId={tid}",
        ]
    )
