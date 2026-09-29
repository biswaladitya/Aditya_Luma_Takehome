###My thoughts


There is some sort of messy user workflow that simulates the generation of captions w.r.t to these images. We need to figure out how to turn this into a process that the user can easily follow. 

What is their operational workflow:

1. The catalog is one shared CSV file where each row is a product. One of the fields within the row is a link to a white background photo of their product. 

2. Someone updates the catalog with a description of how the static, white backgrounded photo, should evolve into something more dynamic. 

3. Most descriptions are not automatically or quickly captured within the catalog csv. Instead, the generated descriptions are disparate, left within slack or emails, etc. 

4. at some frequency, the image descriptions are centralized by Ellie. 

5. Upon being centralized by ellie, they are sent to a professional photographer who turns the static white background images into something that matches the description. 

6. Once the photos are sent back to Ellie, she gathers feedback from the team and ultimately makes her picture. 

7. The finalized pictures are downloaded into a shared google drive. 

8. The finalized pictures are then uploaded onto the website. 

Previously tried approaches that failed: 

1. creative automation tool with a nice dashboard (probably representing a flow or some sort of logical set of steps) that would do this. the problem is that no one logged in. 

Acceptance criteria - there needs to be some sort of ai automation tool that fits into the user's operational workflow. ideally, we wouldnt want to add more work for them, or make them use another platform or website to do all this, although it's quite natural and did not previously work. 

maybe we can build some sort of software that directly connects to google sheets/docs, slack, and gmail and autonoumously does the following: 

1. read whatever is inside google docs/sheets
2. send out slack messages and create threads where people can input their catalog descriptions. 

let's break down some of the tasks a bit more: 

1. what are the smaller sub-problems that need to be solved. 

- there needs to be some way of creating image descriptions and populating it within the sheet. Right now it's done manually by hand

- there needs to be some way of going from sheet descriptions -> generated images. 

- subsequently, those generated images need to live in google drive somewhere. 

- finally, Ellie must approve or deny the pictures - depending on the approvals and denials those pictures may need to be re-generated. 

2. who is our end/target audience? 

- Since it's Ellie, we need to target the various parts of her job: 

1. centralization of feedback. 
2. image generation (what used to be a human photographer is now an AI)
3. forwarding into slack for opinions
3. approval and denial process. 
4. (optional) - a regeneration of the images based off the feedback - ex: everyone denies everything. This would mean we loop back to step 2
5. uploading the generated pics (clearly labeled by the product SKU) back into google drive. 


Here's my two cents: 

1. There are two distinct problems to solve - the first is figuring out a good way of centralizing the shot ideas. Ideally ellie shouldn't have to manually scrape through slack and emails to figure out how people wish to bring their photos to life with a shot idea. 

2. Let's say that problem 1 is fully solved and each csv contains a shot idea, then we can create a platform where someone logs in, connects to their google drive, and then uploads the CSV. Once the CSV is uploaded, we can have two user profiles -> for Maya she can view both a dashboard showing the distribution of pictures and their respective states. For Ellie, she can actually generate pictures and then send them out to a dedicated slack channel where the feedback can be collected. Once the feedback is collected, there would need to be some way of taking that feedback and putting it back into our platform upon which things can be mass uploaded into google drive. 


3. i'd need to figure out - what APIs can make connectivity like this exist/happen?

- i'll probably start by focusing on problem 2. solving problem 1 is a bit harder without making Ellie & all other creators actually add in their two cents through the platform too? if they were capable of doing that, they'd just do it in google sheets, so the real bottle-neck is the personal side of the operational workflow


