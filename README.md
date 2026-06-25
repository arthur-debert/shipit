# shipit

A small set of utilities and agent harnesses to standardize work across my portfolio of personal projects (arthur-debert, lex-fmt and phos-editor on gh)

## Scope

1.  Provisioning:  
2.  Ensure that os level dependencies are installed in a pinned version. 
3.  Development Workflow:  
    1.  The how to full for development workflow. 
    2.  The skills for development (shipt-to-PRD, shipit-to-issues, shipt-grill-with-docs) 
4.  Standarized Linting and Formatting via LeftHook 
    2.1 Multi language / file types supported (rust, python, shell, markdown, yaml, json, go, lex)
5.  Github Repo Setup:  
    1.  Ensure used issues labels are available 
    2.  Ensures a standarized ruleset for branches  
    3.  Ensures repo secrets are available (fetched from local doppler config) 
6.  Tooling:  
    4.1 Leverages pixi to offer pixi build, lint, test, and run commands for all supported languages.
7.  PR and Code Reviews 
    A significant part of the value is the connection between skills, tooling, and review helpers that take the guesswork out of agents on how to handle it. 

##   How It  Works

1\. Install : $ pixi install shipit \<path\> --push

1.  Provisions the project 
2.  Runs the script that setup gh repo. 
3.  Copy the skills.  
4.  Copy the left hook config  
5.  Stores a .shipit.toml file with the commit hash of the shipit repo that installed it. 
6.  Sets up git commit hooks to run left hook on pre-commit and pre-push. 
7.  Adds to AGENTS.md a section on how the development workflow works (AGENTS.lex ) and a short pixi command reference for shipit commands. 

  This is the same command for fresh installs and updates. 

  For updates: shipt will compare the files to be repalced in the consumer repo with the same file shipit had at the last time shipit ran on that consumer repo. If there are changes in the consumer repo, shipit will prompt the user to either overwrite or skip the file.

That is, install is incremental and mostly safe (if doppler secrets change between applies the new one will be set, which is most likely the desired behavior). 

If push is set it will push to the repo's main bypassing PR workflow via ADMIN . 

##   2. PR Reviews

2.1 pixil shipit-request-review \<pr\_number\> \<reviewer\>....

1.  This pixi extension will look into the projects .shipit.toml file to read which reviewers are used in the project , for now we have copilot, agy-local, and codex-local.  
2.  If no reviewers are specified, it will use the default reviewers in the .shipit.toml file. 

2.2. pixi shipit-pr-status

  Returns a json summary of the PR status according to our developement workflow: with fields for reviews\_complete (each can be complete ,  pending, addressing needed or not needed), ci cheks (status and if failed, which ones), mergeable. 

  2.2 pixi shipit-pr-next-action

  A state machine that encodes the rules for the workflow development and runs the next action (request or re requrest reviews, check for ready status, if can be ready, flips the PR from draft to ready, etc).  Returns a useful json summary of the action taken and current status. Implemented on top of shipit-pr-status and shipit-request-review.
