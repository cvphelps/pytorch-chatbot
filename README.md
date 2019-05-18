# PyTorch Chatbot
This is a simple chatbot based on seq2seq and implemented in PyTorch.

## Requirements
* python 3.5+
* pytorch 0.4.0+
* tqdm
* tensorboardX
* apex(optional)

#### Setup
Clone the repository. 

#### Corpus
The dataset directory should look like this:
```
<data>
├── <train.txt>
├── <valid.txt>
|       ⋮
└── <test.txt>
```

Corpus files are text files with pairs of conversational exchanges. Each input-output pair is separated by a new line. For example:
```
I'll see you next time.
Sure. Bye.
How are you?
Better than ever.
```

#### Preprocessing
Preprocess the corpus text files before training.
```
python preprocess.py --config <config_path>
```

#### Training
Run this command to start training, change the argument values in your own need.
```
python main.py --config <config_path>
```
Continue training with saved checkpoint.
```
python main.py --config <config_path> --load <checkpoint_path>
```
Run tensorboardX to see the training result.
```
tensorboard --logdir <log_path>
```
For more options,
```
python main.py -h
```

#### Testing
Evaluate the saved model with input sequences in the test corpus.
```
python main.py --config <config_path> -te
```

#### TODO
- [ ] beam search (already implemented in master branch)
- [ ] test multi gpu


