
Train pascal:
CUDA_VISIBLE_DEVICES=0 python trainval_net.py --dataset pascal_voc --net res101  --bs 1 --nw 2 --lr 0.001 --lr_decay_step 5 --cuda --use_tfb

Tensorboard:
tensorboard --logdir logs

Train df
CUDA_VISIBLE_DEVICES=0 python trainval_net.py --dataset df --net res101  --bs 1 --nw 4 --lr 0.001 --lr_decay_step 5 --cuda --use_tfb