# JobAgent privacy policy

JobAgent is a personal tool used only by its author, Haatim Ali, to track his own internship applications.

**What it accesses.** With the author's sign-in, JobAgent reads his own Gmail inbox through the Gmail API using the read-only scope (`gmail.readonly`). It cannot send, delete, label or move email.

**What it does with it.** It looks at recruiting emails (application confirmations, rejections, assessment and interview invitations) to update the author's private Google Sheet of applications and to post a short summary in his private Discord server. Email text is sent to the Anthropic API only to classify each message; it is not stored there for training.

**What it keeps.** For each email handled: the message ID, date, sender, subject, a one-line summary and the resulting tracker change, in a private database on the author's server. Email bodies are not stored.

**Sharing.** No data is sold or shared with anyone else. JobAgent has no other users.

**Revoking access.** Access can be removed at any time at https://myaccount.google.com/permissions.

Contact: htali@ucsc.edu
