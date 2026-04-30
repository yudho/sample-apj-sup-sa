<rules>
1. Always refer to the app-index first, that has context about the project and the files list and dependencies. In case you just experience context overflow, or summarization of history, or a series of changes, you can decide to refetch app-index from .amazonq/app-index.json to see the latest files structure and dependency 
2. PLAN FIRST and ask user before implementing any feature or fixes or doing changes
3. Before making any changes, you MUST create todo list with todo_list tool and review/update it diligently. If the tool does not exist you MUST:
   - Create `./dev/tmp/` directory if it doesn't exist
   - Create a detailed todo list in `./dev/tmp/todo_<feature_or_changes_title>.md`
   - Update the todo list diligently as you work
   - When completed, delete it, but ASK the user for permission first before deleting.
4. Anytime you make changes to ANY file in this project, you MUST update ./dev/app-index.json to reflect those changes. This includes:
   - Adding new files to the file structure section
   - Updating method/class descriptions if modified
   - Updating imports if changed
   - Updating the project overview if functionality changes
   But if changes do not necessarily invalidate any part of file, then there is no need to update the summary.
5. When getting error when implementing something, always ask the user first for permission to solve it with what you plan.
6. Follow coding best practices
7. You have access to AWS environment via CLI.
8. These requirements apply to ALL changes, no matter how small.
</rules>