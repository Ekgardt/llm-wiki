# Queue task lineage

`llm_wiki.queue_task_lineage` answers what one queue task inherits when it is redriven: the compile lineage, the capture decision and the seals that bind them. It reads rows through the connection it is given and raises the queue errors; it never commits and owns no table. `queue_core` keeps the SQL that writes those rows and imports the names it calls.

Own tests are under `tests/modules/queue_task_lineage`. The owned files and the complete maintenance
context are listed in the `-context.json` manifest next to this guide.
